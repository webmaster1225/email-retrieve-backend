from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.account import MailboxAccount
from app.models.sync import AuthToken, SyncRun
from app.services.connectors.base import AccountStatusView, SyncPageResult
from app.services.connectors.graph_connector import plain_sync_label
from app.services.gmail_client import GmailAuthError, GmailClient


class GmailMailboxConnector:
    """Real Gmail API connector for Northwyn."""

    def __init__(self, db: Session, account_id: str = "northwyn"):
        self.db = db
        self.account_id = account_id
        self.gmail = GmailClient(db, account_id=account_id)

    def _account(self) -> MailboxAccount:
        row = self.db.get(MailboxAccount, self.account_id)
        if not row:
            raise RuntimeError(f"Unknown mailbox account: {self.account_id}")
        return row

    def _active_sync_run(self) -> SyncRun | None:
        return (
            self.db.query(SyncRun)
            .filter(SyncRun.account_id == self.account_id, SyncRun.status == "running")
            .order_by(SyncRun.started_at.desc())
            .first()
        )

    def status(self) -> AccountStatusView:
        account = self._account()
        token = self.gmail.get_token_row()
        if not token or str(token.access_token).startswith("stub-"):
            if token and str(token.access_token).startswith("stub-"):
                account.status = "not_connected"
                self.db.commit()
            return AccountStatusView(
                account_id=self.account_id,
                status="not_connected",
                connected=False,
                last_sync_at=account.last_sync_at,
                last_sync_plain=account.last_sync_plain,
                permissions={},
                plain_message="Not connected — sign in with Google",
                is_stub=False,
            )
        try:
            self.gmail.ensure_access_token()
            account.is_stub = False
            perms = account.permissions_json or {
                "read_mail": True,
                "send": True,
                "calendar": False,
                "drafts": True,
            }
            account.permissions_json = perms
            if self._active_sync_run():
                account.status = "syncing"
                self.db.commit()
                return AccountStatusView(
                    account_id=self.account_id,
                    status="syncing",
                    connected=True,
                    last_sync_at=account.last_sync_at,
                    last_sync_plain=account.last_sync_plain,
                    permissions=perms,
                    plain_message="Syncing…",
                    can_send=bool(perms.get("send")),
                    is_stub=False,
                    partial_permissions=False,
                )
            account.status = "connected"
            self.db.commit()
            return AccountStatusView(
                account_id=self.account_id,
                status="connected",
                connected=True,
                last_sync_at=account.last_sync_at,
                last_sync_plain=account.last_sync_plain,
                permissions=perms,
                plain_message="Connected",
                can_send=bool(perms.get("send")),
                is_stub=False,
                partial_permissions=False,
            )
        except GmailAuthError:
            account.status = "reconnect_needed"
            self.db.commit()
            return AccountStatusView(
                account_id=self.account_id,
                status="reconnect_needed",
                connected=False,
                last_sync_at=account.last_sync_at,
                last_sync_plain=account.last_sync_plain,
                permissions=account.permissions_json or {},
                plain_message="Northwyn's mailbox connection expired — one click to reconnect",
                is_stub=False,
            )

    def get_login_url(self) -> dict:
        try:
            url = self.gmail.get_auth_url(state=f"account:{self.account_id}")
            return {"stub": False, "login_url": url, "account_id": self.account_id}
        except GmailAuthError as exc:
            return {"stub": False, "error": str(exc), "account_id": self.account_id}

    async def handle_oauth_callback_async(self, code: str) -> AccountStatusView:
        token_row = self.gmail.exchange_code(code)
        profile = await self.gmail.fetch_profile(token_row.access_token)
        signed_in = (
            profile.get("emailAddress") or profile.get("email") or ""
        ).strip()
        token_row.user_email = signed_in or None
        token_row.user_id = profile.get("id") or signed_in or None
        token_row.account_id = self.account_id
        account = self._account()
        expected = (account.email or "").strip().lower()
        actual = signed_in.lower()
        if expected and actual and expected != actual:
            self.db.query(AuthToken).filter(AuthToken.account_id == self.account_id).delete()
            account.status = "not_connected"
            account.permissions_json = {}
            account.updated_at = datetime.utcnow()
            self.db.commit()
            raise GmailAuthError(
                f"Signed in as <{signed_in}>, but {account.display_name} expects <{account.email}>. "
                "Choose the matching Google account, then try again."
            )
        account.status = "connected"
        account.is_stub = False
        account.permissions_json = {
            "read_mail": True,
            "send": True,
            "calendar": False,
            "drafts": True,
        }
        account.updated_at = datetime.utcnow()
        self.db.commit()
        return self.status()

    def disconnect(self) -> dict:
        self.db.query(AuthToken).filter(AuthToken.account_id == self.account_id).delete()
        account = self._account()
        account.status = "not_connected"
        account.permissions_json = {}
        account.is_stub = False
        account.updated_at = datetime.utcnow()
        self.db.commit()
        return {
            "connected": False,
            "account_id": self.account_id,
            "consequences": (
                f"Disconnected {account.display_name}. Synced messages tagged "
                f"source_account={self.account_id} remain until deleted. "
                "Other mailboxes are unaffected."
            ),
        }

    def list_messages(self, folder: str = "sent", *, cursor: str | None = None) -> SyncPageResult:
        return SyncPageResult(values=[], done=True)

    def get_thread(self, thread_id: str) -> dict:
        return {"thread_id": thread_id, "messages": []}

    def list_events(self, **kwargs) -> list:
        return []

    def get_contacts(self, **kwargs) -> list:
        return []

    def create_draft(self, **kwargs) -> dict:
        raise NotImplementedError("Gmail draft creation lands in a later phase")

    def send(self, **kwargs) -> dict:
        raise NotImplementedError("Sending stays hard-gated in later phases")

    def start_stub_sync(self, sync_type: str = "full") -> SyncRun:
        raise RuntimeError("Stub sync removed — use real account sync")

    def mark_token_fresh(self) -> None:
        account = self._account()
        now = datetime.utcnow()
        account.last_sync_at = now
        account.last_sync_plain = plain_sync_label(now)
        account.status = "connected"
        account.updated_at = now
        self.db.commit()
