from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import msal
import requests
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.sync import AuthToken

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MESSAGE_SELECT = (
    "id,subject,sentDateTime,toRecipients,ccRecipients,bccRecipients,"
    "bodyPreview,conversationId,internetMessageId,webLink,hasAttachments,"
    "from,sender,importance,categories"
)
CONTACT_MESSAGE_SELECT = (
    "id,subject,sentDateTime,toRecipients,ccRecipients,bccRecipients,"
    "bodyPreview,conversationId,webLink,hasAttachments"
)
INBOX_MESSAGE_SELECT = (
    "id,subject,receivedDateTime,sentDateTime,from,sender,toRecipients,"
    "bodyPreview,conversationId,internetMessageId,webLink,hasAttachments,"
    "importance,categories"
)


class GraphAuthError(Exception):
    pass


def parse_token_scopes(access_token: str) -> list[str]:
    import base64
    import json

    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        scp = data.get("scp") or data.get("scope") or ""
        if isinstance(scp, list):
            return scp
        return [s for s in str(scp).split() if s]
    except Exception:
        return []


def _msal_http_client() -> requests.Session:
    """Bypass system proxy settings that can block login.microsoftonline.com."""
    session = requests.Session()
    session.trust_env = False
    return session


_shared_http: httpx.AsyncClient | None = None


async def get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http
    if _shared_http is None or _shared_http.is_closed:
        _shared_http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=12, max_connections=20),
        )
    return _shared_http


class GraphClient:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self._msal_app: msal.ConfidentialClientApplication | None = None

    @property
    def msal_app(self) -> msal.ConfidentialClientApplication:
        if self._msal_app is None:
            try:
                self._msal_app = msal.ConfidentialClientApplication(
                    self.settings.azure_client_id,
                    authority=f"https://login.microsoftonline.com/{self.settings.azure_tenant_id}",
                    client_credential=self.settings.azure_client_secret,
                    http_client=_msal_http_client(),
                )
            except requests.exceptions.RequestException as exc:
                raise GraphAuthError(
                    "Could not reach Microsoft login service. Check your internet connection "
                    "and disable VPN/proxy if enabled."
                ) from exc
        return self._msal_app

    def get_auth_url(self, state: str | None = None) -> str:
        return self.msal_app.get_authorization_request_url(
            scopes=self.settings.graph_scope_list,
            redirect_uri=self.settings.azure_redirect_uri,
            state=state,
        )

    def exchange_code(self, code: str) -> AuthToken:
        result = self.msal_app.acquire_token_by_authorization_code(
            code,
            scopes=self.settings.graph_scope_list,
            redirect_uri=self.settings.azure_redirect_uri,
        )
        if "error" in result:
            raise GraphAuthError(result.get("error_description") or result["error"])
        return self._store_token(result)

    def _store_token(self, result: dict) -> AuthToken:
        expires_in = result.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 60)
        existing = self.db.query(AuthToken).order_by(AuthToken.updated_at.desc()).first()
        token_row = existing or AuthToken(access_token="")
        token_row.access_token = result["access_token"]
        token_row.refresh_token = result.get("refresh_token") or token_row.refresh_token
        token_row.expires_at = expires_at
        token_row.updated_at = datetime.utcnow()
        if existing is None:
            self.db.add(token_row)
        self.db.commit()
        self.db.refresh(token_row)
        return token_row

    def get_token_row(self) -> AuthToken | None:
        return self.db.query(AuthToken).order_by(AuthToken.updated_at.desc()).first()

    def ensure_access_token(self) -> str:
        token_row = self.get_token_row()
        if not token_row:
            raise GraphAuthError("Not authenticated. Sign in with Microsoft first.")

        now = datetime.now(timezone.utc)
        expires_at = token_row.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at and expires_at > now:
            return token_row.access_token

        if not token_row.refresh_token:
            raise GraphAuthError("Session expired. Please sign in again.")

        result = self.msal_app.acquire_token_by_refresh_token(
            token_row.refresh_token,
            scopes=self.settings.graph_scope_list,
        )
        if "error" in result:
            raise GraphAuthError(result.get("error_description") or result["error"])
        token_row = self._store_token(result)
        return token_row.access_token

    async def fetch_profile(self, access_token: str) -> dict:
        client = await get_shared_http_client()
        response = await client.get(
            f"{GRAPH_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()

    async def fetch_message_body(self, graph_message_id: str) -> dict:
        access_token = self.ensure_access_token()
        url = f"{GRAPH_BASE}/me/messages/{graph_message_id}?$select=subject,body,sentDateTime"
        headers = {"Authorization": f"Bearer {access_token}"}
        client = await get_shared_http_client()
        response = await client.get(url, headers=headers)
        if response.status_code == 429:
            await asyncio.sleep(int(response.headers.get("Retry-After", "5")))
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    async def fetch_sent_items_folder(self) -> dict:
        """Return Sent Items folder metadata including totalItemCount from Outlook."""
        access_token = self.ensure_access_token()
        url = f"{GRAPH_BASE}/me/mailFolders/sentitems?$select=displayName,totalItemCount,unreadItemCount"
        headers = {"Authorization": f"Bearer {access_token}"}
        client = await get_shared_http_client()
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()

    async def fetch_messages_page(
        self,
        url: str | None = None,
        *,
        top: int = 100,
        newest_first: bool = True,
    ) -> dict:
        access_token = self.ensure_access_token()
        if url is None:
            order = "sentDateTime desc" if newest_first else "sentDateTime asc"
            url = (
                f"{GRAPH_BASE}/me/mailFolders/sentitems/messages"
                f"?$select={CONTACT_MESSAGE_SELECT}&$orderby={order}&$top={top}"
            )
        headers = {"Authorization": f"Bearer {access_token}"}
        client = await get_shared_http_client()
        for attempt in range(5):
            response = await client.get(url, headers=headers)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Graph API rate limit exceeded after retries")

    async def fetch_inbox_page(self, url: str | None = None) -> dict:
        access_token = self.ensure_access_token()
        if url is None:
            url = (
                f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
                f"?$select={INBOX_MESSAGE_SELECT}&$orderby=receivedDateTime asc&$top=100"
            )
        headers = {"Authorization": f"Bearer {access_token}"}
        client = await get_shared_http_client()
        for attempt in range(5):
            response = await client.get(url, headers=headers)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                await asyncio.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("Graph API rate limit exceeded after retries")

    async def search_messages_with_participant(self, email: str, *, top: int = 50) -> list[dict]:
        """Search all mail folders for messages involving a contact email."""
        access_token = self.ensure_access_token()
        safe_email = email.replace('"', "")
        url = (
            f"{GRAPH_BASE}/me/messages"
            f"?$search=\"participants:{safe_email}\""
            f"&$select={MESSAGE_SELECT}"
            f"&$top={top}"
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "ConsistencyLevel": "eventual",
        }
        client = await get_shared_http_client()
        for attempt in range(5):
            response = await client.get(url, headers=headers)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                await asyncio.sleep(retry_after)
                continue
            if response.status_code in (400, 501):
                return []
            response.raise_for_status()
            return response.json().get("value") or []
        raise RuntimeError("Graph API rate limit exceeded after retries")

    async def send_mail(
        self,
        *,
        to_email: str,
        to_name: str | None,
        subject: str,
        body: str,
        content_type: str = "Text",
    ) -> None:
        access_token = self.ensure_access_token()
        recipient = {"emailAddress": {"address": to_email}}
        if to_name:
            recipient["emailAddress"]["name"] = to_name
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": content_type, "content": body},
                "toRecipients": [recipient],
            },
            "saveToSentItems": True,
        }
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(f"{GRAPH_BASE}/me/sendMail", json=payload, headers=headers)
            if response.status_code == 403:
                raise GraphAuthError(
                    "Mail.Send permission required. Reconnect Outlook after adding Mail.Send in Azure AD."
                )
            response.raise_for_status()
