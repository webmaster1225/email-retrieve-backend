from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.sync import AuthToken

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v2/userinfo"
GMAIL_PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

DEFAULT_GMAIL_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GmailAuthError(Exception):
    pass


class GmailClient:
    """OAuth + token storage for Northwyn (Gmail API)."""

    def __init__(self, db: Session, account_id: str = "northwyn"):
        self.db = db
        self.account_id = account_id
        self.settings = get_settings()

    def _require_creds(self) -> tuple[str, str, str]:
        s = self.settings
        if not s.google_client_id or not s.google_client_secret:
            raise GmailAuthError(
                "Google OAuth app not configured. Create an OAuth client in Google Cloud Console "
                "and set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in backend/.env "
                "(app credentials — not your Gmail password). Connect still signs you in via the browser."
            )
        redirect = s.google_redirect_uri or (
            f"{s.api_public_base.rstrip('/')}/api/v1/accounts/northwyn/oauth/callback"
        )
        return s.google_client_id, s.google_client_secret, redirect

    @property
    def scope_list(self) -> list[str]:
        raw = self.settings.google_scopes.strip()
        if raw:
            return [s for part in raw.replace(",", " ").split() if (s := part.strip())]
        return list(DEFAULT_GMAIL_SCOPES)

    def get_auth_url(self, state: str | None = None) -> str:
        client_id, _, redirect = self._require_creds()
        params = {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(self.scope_list),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "login_hint": "dbains@northwyn.com",
        }
        if state:
            params["state"] = state
        return f"{GOOGLE_AUTH}?{urlencode(params)}"

    def exchange_code(self, code: str) -> AuthToken:
        client_id, client_secret, redirect = self._require_creds()
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            response = client.post(
                GOOGLE_TOKEN,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect,
                    "grant_type": "authorization_code",
                },
            )
            if response.status_code >= 400:
                detail = response.text
                try:
                    detail = response.json().get("error_description") or response.json().get("error") or detail
                except Exception:
                    pass
                raise GmailAuthError(f"Google token exchange failed: {detail}")
            result = response.json()
        return self._store_token(result)

    def _store_token(self, result: dict) -> AuthToken:
        expires_in = int(result.get("expires_in", 3600))
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        existing = (
            self.db.query(AuthToken)
            .filter(AuthToken.account_id == self.account_id)
            .order_by(AuthToken.updated_at.desc())
            .first()
        )
        token_row = existing or AuthToken(access_token="", account_id=self.account_id)
        token_row.account_id = self.account_id
        token_row.access_token = result["access_token"]
        if result.get("refresh_token"):
            token_row.refresh_token = result["refresh_token"]
        elif not token_row.refresh_token:
            token_row.refresh_token = None
        token_row.expires_at = expires_at
        token_row.updated_at = datetime.utcnow()
        if existing is None:
            self.db.add(token_row)
        self.db.commit()
        self.db.refresh(token_row)
        return token_row

    def get_token_row(self) -> AuthToken | None:
        return (
            self.db.query(AuthToken)
            .filter(AuthToken.account_id == self.account_id)
            .order_by(AuthToken.updated_at.desc())
            .first()
        )

    def ensure_access_token(self) -> str:
        token_row = self.get_token_row()
        if not token_row:
            raise GmailAuthError("Not authenticated. Sign in with Google first.")
        if str(token_row.access_token).startswith("stub-"):
            raise GmailAuthError("Stub connection removed. Sign in with Google again.")

        now = datetime.now(timezone.utc)
        expires_at = token_row.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at and expires_at > now:
            return token_row.access_token

        if not token_row.refresh_token:
            raise GmailAuthError("Session expired. Please sign in with Google again.")

        client_id, client_secret, _ = self._require_creds()
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            response = client.post(
                GOOGLE_TOKEN,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": token_row.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code >= 400:
                raise GmailAuthError("Google session expired. Please sign in again.")
            result = response.json()
        # Preserve refresh_token if Google omits it on refresh
        if not result.get("refresh_token"):
            result["refresh_token"] = token_row.refresh_token
        token_row = self._store_token(result)
        return token_row.access_token

    async def fetch_profile(self, access_token: str | None = None) -> dict:
        token = access_token or self.ensure_access_token()
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            user = await client.get(
                GOOGLE_USERINFO,
                headers={"Authorization": f"Bearer {token}"},
            )
            profile: dict = {}
            if user.status_code < 400:
                profile = user.json()
            gmail = await client.get(
                GMAIL_PROFILE,
                headers={"Authorization": f"Bearer {token}"},
            )
            if gmail.status_code < 400:
                data = gmail.json()
                profile["emailAddress"] = data.get("emailAddress") or profile.get("email")
            return profile

    async def create_draft(
        self,
        *,
        to_email: str,
        to_name: str | None,
        subject: str,
        body: str,
    ) -> dict:
        """Create a Gmail draft (Gate 6)."""
        import base64
        from email.mime.text import MIMEText

        token = self.ensure_access_token()
        message = MIMEText(body or "")
        message["to"] = f"{to_name} <{to_email}>" if to_name else to_email
        message["subject"] = subject or ""
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                headers={"Authorization": f"Bearer {token}"},
                json={"message": {"raw": raw}},
            )
            if response.status_code >= 400:
                raise GmailAuthError(f"Gmail draft create failed: {response.text[:200]}")
            data = response.json()
            return {"id": data.get("id"), "web_link": None}

    async def send_mail(
        self,
        *,
        to_email: str,
        to_name: str | None,
        subject: str,
        body: str,
    ) -> dict:
        import base64
        from email.mime.text import MIMEText

        token = self.ensure_access_token()
        message = MIMEText(body or "")
        message["to"] = f"{to_name} <{to_email}>" if to_name else to_email
        message["subject"] = subject or ""
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {token}"},
                json={"raw": raw},
            )
            if response.status_code >= 400:
                raise GmailAuthError(f"Gmail send failed: {response.text[:200]}")
            data = response.json()
            return {
                "id": data.get("id"),
                "conversation_id": data.get("threadId"),
                "internet_message_id": data.get("id"),
            }
