from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

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
    def __init__(self, db: Session, account_id: str = "edge"):
        self.db = db
        self.account_id = account_id
        self.settings = get_settings()
        self._msal_app: msal.ConfidentialClientApplication | None = None
        self._client_id, self._client_secret, self._tenant_id, self._redirect_uri = (
            self._resolve_azure_app()
        )

    def _resolve_azure_app(self) -> tuple[str, str, str, str]:
        """Resolve OAuth *app* credentials (not the user's email password).

        Users always sign in via browser redirect. .env only holds the Azure AD
        app registration (client id/secret) already used for Edge.
        """
        s = self.settings
        client_id = s.azure_client_id or ""
        client_secret = s.azure_client_secret or ""

        if self.account_id in ("galaxy", "careers"):
            # Prefer a dedicated Galaxy-tenant app if configured.
            # Otherwise reuse the Edge app with /common (NOT the Edge tenant GUID).
            # Using the Edge single-tenant GUID here causes:
            # "application has not been installed by the administrator of the tenant".
            client_id = s.galaxy_azure_client_id or client_id
            client_secret = s.galaxy_azure_client_secret or client_secret
            if s.galaxy_azure_client_id and s.galaxy_azure_tenant_id:
                tenant_id = s.galaxy_azure_tenant_id
            else:
                tenant_id = "common"
            redirect = (
                s.galaxy_azure_redirect_uri
                or f"{s.api_public_base.rstrip('/')}/api/v1/accounts/{self.account_id}/oauth/callback"
            )
            return client_id, client_secret, tenant_id, redirect

        redirect = s.azure_redirect_uri
        return (
            client_id,
            client_secret,
            s.azure_tenant_id or "common",
            redirect,
        )

    def _ensure_configured(self) -> None:
        if not self._client_id or not self._client_secret:
            raise GraphAuthError(
                "Microsoft app not configured. Set AZURE_CLIENT_ID and AZURE_CLIENT_SECRET "
                "in backend/.env (Azure AD app registration — not your email password). "
                "Connect still signs you in via the browser."
            )

    def redirect_uri_for_account_oauth(self) -> str:
        """Callback used by /api/v1/accounts/{id}/oauth/callback."""
        s = self.settings
        if self.account_id in ("galaxy", "careers"):
            return (
                s.galaxy_azure_redirect_uri
                or f"{s.api_public_base.rstrip('/')}/api/v1/accounts/{self.account_id}/oauth/callback"
            )
        # Edge Settings uses the same redirect already registered for Contacts login
        return s.azure_redirect_uri or (
            f"{s.api_public_base.rstrip('/')}/api/v1/auth/callback"
        )

    @property
    def msal_app(self) -> msal.ConfidentialClientApplication:
        self._ensure_configured()
        if self._msal_app is None:
            try:
                self._msal_app = msal.ConfidentialClientApplication(
                    self._client_id,
                    authority=f"https://login.microsoftonline.com/{self._tenant_id}",
                    client_credential=self._client_secret,
                    http_client=_msal_http_client(),
                )
            except requests.exceptions.RequestException as exc:
                raise GraphAuthError(
                    "Could not reach Microsoft login service. Check your internet connection "
                    "and disable VPN/proxy if enabled."
                ) from exc
        return self._msal_app

    def get_auth_url(self, state: str | None = None, *, redirect_uri: str | None = None) -> str:
        """Build Microsoft login URL — user enters email/password in the browser."""
        kwargs = {
            "scopes": self.settings.graph_scope_list,
            "redirect_uri": redirect_uri or self._redirect_uri,
            "state": state,
            "login_hint": self._login_hint(),
            "prompt": "select_account",
        }
        try:
            return self.msal_app.get_authorization_request_url(**kwargs)
        except (ValueError, GraphAuthError, TypeError) as exc:
            raise GraphAuthError(str(exc)) from exc

    def _login_hint(self) -> str | None:
        hints = {
            "edge": "dbains@edgeinvesting.ca",
            "galaxy": "dalbir.bains@galaxypharma.net",
            "careers": "careers@galaxypharma.net",
        }
        return hints.get(self.account_id)

    def exchange_code(self, code: str, *, redirect_uri: str | None = None) -> AuthToken:
        result = self.msal_app.acquire_token_by_authorization_code(
            code,
            scopes=self.settings.graph_scope_list,
            redirect_uri=redirect_uri or self._redirect_uri,
        )
        if "error" in result:
            raise GraphAuthError(result.get("error_description") or result["error"])
        return self._store_token(result)

    def _store_token(self, result: dict) -> AuthToken:
        expires_in = result.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in) - 60)
        existing = (
            self.db.query(AuthToken)
            .filter(AuthToken.account_id == self.account_id)
            .order_by(AuthToken.updated_at.desc())
            .first()
        )
        if existing is None and self.account_id == "edge":
            existing = (
                self.db.query(AuthToken)
                .filter((AuthToken.account_id.is_(None)) | (AuthToken.account_id == ""))
                .order_by(AuthToken.updated_at.desc())
                .first()
            )
        token_row = existing or AuthToken(access_token="", account_id=self.account_id)
        token_row.account_id = self.account_id
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
        """Return only this mailbox's token — never borrow another account's OAuth identity."""
        row = (
            self.db.query(AuthToken)
            .filter(AuthToken.account_id == self.account_id)
            .order_by(AuthToken.updated_at.desc())
            .first()
        )
        if row:
            return row
        # Legacy Edge rows may predate account_id; only adopt unscoped tokens.
        if self.account_id == "edge":
            return (
                self.db.query(AuthToken)
                .filter((AuthToken.account_id.is_(None)) | (AuthToken.account_id == ""))
                .order_by(AuthToken.updated_at.desc())
                .first()
            )
        return None

    def ensure_access_token(self) -> str:
        token_row = self.get_token_row()
        if not token_row:
            raise GraphAuthError("Not authenticated. Sign in with Microsoft first.")
        if str(token_row.access_token).startswith("stub-"):
            raise GraphAuthError("Stub connection removed. Sign in with Microsoft again.")

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
        since: datetime | None = None,
    ) -> dict:
        access_token = self.ensure_access_token()
        if url is None:
            if since is not None:
                since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
                since_iso = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                filter_expr = urlencode({"$filter": f"sentDateTime ge {since_iso}"})
                url = (
                    f"{GRAPH_BASE}/me/mailFolders/sentitems/messages"
                    f"?$select={CONTACT_MESSAGE_SELECT}&{filter_expr}"
                    f"&$orderby=sentDateTime desc&$top={top}"
                )
            else:
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

    async def fetch_inbox_page(
        self,
        url: str | None = None,
        *,
        since: datetime | None = None,
    ) -> dict:
        access_token = self.ensure_access_token()
        if url is None:
            if since is not None:
                since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
                since_iso = since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                filter_expr = urlencode({"$filter": f"receivedDateTime ge {since_iso}"})
                url = (
                    f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
                    f"?$select={INBOX_MESSAGE_SELECT}&{filter_expr}"
                    f"&$orderby=receivedDateTime desc&$top=100"
                )
            else:
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
    ) -> dict:
        """Create then send so we can capture conversation/message ids for reply matching."""
        access_token = self.ensure_access_token()
        recipient = {"emailAddress": {"address": to_email}}
        if to_name:
            recipient["emailAddress"]["name"] = to_name
        payload = {
            "subject": subject,
            "body": {"contentType": content_type, "content": body},
            "toRecipients": [recipient],
        }
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            created = await client.post(
                f"{GRAPH_BASE}/me/messages",
                json=payload,
                headers=headers,
            )
            if created.status_code == 403:
                raise GraphAuthError(
                    "Mail.Send permission required. Reconnect Outlook after adding Mail.Send in Azure AD."
                )
            created.raise_for_status()
            data = created.json()
            msg_id = data.get("id")
            meta = {
                "id": msg_id,
                "conversation_id": data.get("conversationId"),
                "internet_message_id": data.get("internetMessageId"),
                "web_link": data.get("webLink"),
            }
            if msg_id:
                sent = await client.post(
                    f"{GRAPH_BASE}/me/messages/{msg_id}/send",
                    headers=headers,
                )
                if sent.status_code == 403:
                    raise GraphAuthError(
                        "Mail.Send permission required. Reconnect Outlook after adding Mail.Send in Azure AD."
                    )
                sent.raise_for_status()
            return meta

    async def create_draft(
        self,
        *,
        to_email: str,
        to_name: str | None,
        subject: str,
        body: str,
        content_type: str = "Text",
    ) -> dict:
        """Create a draft in the signed-in mailbox (Gate 6)."""
        access_token = self.ensure_access_token()
        recipient = {"emailAddress": {"address": to_email}}
        if to_name:
            recipient["emailAddress"]["name"] = to_name
        payload = {
            "subject": subject,
            "body": {"contentType": content_type, "content": body},
            "toRecipients": [recipient],
        }
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(
                f"{GRAPH_BASE}/me/messages",
                json=payload,
                headers=headers,
            )
            if response.status_code == 403:
                raise GraphAuthError(
                    "Permission required to create drafts. "
                    "Reconnect Outlook after updating Azure AD permissions."
                )
            response.raise_for_status()
            data = response.json()
            return {
                "id": data.get("id"),
                "web_link": data.get("webLink"),
            }
