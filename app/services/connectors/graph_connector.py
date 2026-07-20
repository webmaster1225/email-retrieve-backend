from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.account import MailboxAccount
from app.models.sync import AuthToken, SyncRun
from app.services.connectors.base import AccountStatusView, SyncPageResult
from app.services.graph_client import GraphAuthError, GraphClient, parse_token_scopes


def plain_sync_label(when: datetime | None) -> str | None:
    """Return e.g. 'up to date 8:02a'."""
    if not when:
        return None
    hour = when.hour % 12 or 12
    minute = when.strftime("%M")
    suffix = "a" if when.hour < 12 else "p"
    return f"up to date {hour}:{minute}{suffix}"


class GraphMailboxConnector:
    """Real Microsoft Graph connector — used for Edge (and later Galaxy)."""

    def __init__(self, db: Session, account_id: str = "edge"):
        self.db = db
        self.account_id = account_id
        self.graph = GraphClient(db, account_id=account_id)

    def _account(self) -> MailboxAccount:
        row = self.db.get(MailboxAccount, self.account_id)
        if not row:
            raise RuntimeError(f"Unknown mailbox account: {self.account_id}")
        return row

    def status(self) -> AccountStatusView:
        account = self._account()
        token = (
            self.db.query(AuthToken)
            .filter(AuthToken.account_id == self.account_id)
            .order_by(AuthToken.updated_at.desc())
            .first()
        )
        if not token:
            return AccountStatusView(
                account_id=self.account_id,
                status="not_connected",
                connected=False,
                last_sync_at=account.last_sync_at,
                last_sync_plain=account.last_sync_plain,
                permissions=account.permissions_json or {},
                plain_message="Not connected",
                is_stub=False,
            )
        if str(token.access_token).startswith("stub-"):
            account.status = "not_connected"
            self.db.commit()
            return AccountStatusView(
                account_id=self.account_id,
                status="not_connected",
                connected=False,
                last_sync_at=account.last_sync_at,
                last_sync_plain=account.last_sync_plain,
                permissions={},
                plain_message="Not connected — sign in with Microsoft",
                is_stub=False,
            )

        try:
            access = self.graph.ensure_access_token()
            scopes = parse_token_scopes(access)
            can_send = "Mail.Send" in scopes
            perms = {
                "read_mail": "Mail.Read" in scopes or True,
                "send": can_send,
                "calendar": "Calendars.Read" in scopes,
                "drafts": True,
            }
            partial = not can_send
            plain = None
            if partial:
                plain = "Connected — can draft, can't send until Mail.Send is granted"
            account.status = "connected"
            account.permissions_json = perms
            self.db.commit()
            return AccountStatusView(
                account_id=self.account_id,
                status="connected",
                connected=True,
                last_sync_at=account.last_sync_at,
                last_sync_plain=account.last_sync_plain,
                permissions=perms,
                plain_message=plain or "Connected",
                can_send=can_send,
                is_stub=False,
                partial_permissions=partial,
            )
        except GraphAuthError:
            account.status = "reconnect_needed"
            self.db.commit()
            return AccountStatusView(
                account_id=self.account_id,
                status="reconnect_needed",
                connected=False,
                last_sync_at=account.last_sync_at,
                last_sync_plain=account.last_sync_plain,
                permissions=account.permissions_json or {},
                plain_message="Connection expired — one click to reconnect",
                is_stub=False,
            )

    def get_login_url(self) -> dict:
        try:
            redirect = self.graph.redirect_uri_for_account_oauth()
            url = self.graph.get_auth_url(
                state=f"account:{self.account_id}",
                redirect_uri=redirect,
            )
            return {"stub": False, "login_url": url, "account_id": self.account_id}
        except GraphAuthError as exc:
            return {"stub": False, "error": str(exc), "account_id": self.account_id}

    async def handle_oauth_callback_async(self, code: str) -> AccountStatusView:
        redirect = self.graph.redirect_uri_for_account_oauth()
        token_row = self.graph.exchange_code(code, redirect_uri=redirect)
        profile = await self.graph.fetch_profile(token_row.access_token)
        token_row.user_email = profile.get("mail") or profile.get("userPrincipalName")
        token_row.user_id = profile.get("id")
        token_row.account_id = self.account_id
        account = self._account()
        account.status = "connected"
        account.is_stub = False
        scopes = parse_token_scopes(token_row.access_token)
        can_send = "Mail.Send" in scopes
        account.permissions_json = {
            "read_mail": True,
            "send": can_send,
            "calendar": "Calendars.Read" in scopes,
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
        account.updated_at = datetime.utcnow()
        self.db.commit()
        return {
            "connected": False,
            "account_id": self.account_id,
            "consequences": (
                f"Disconnected {account.display_name}. Synced messages tagged "
                f"source_account={self.account_id} remain in the database until you delete them. "
                "Other mailboxes are unaffected."
            ),
        }

    def list_messages(self, folder: str = "sent", *, cursor: str | None = None) -> SyncPageResult:
        raise NotImplementedError("Use SyncService for Edge message listing")

    def get_thread(self, thread_id: str) -> dict:
        return {"thread_id": thread_id, "messages": []}

    def list_events(self, **kwargs) -> list:
        return []

    def get_contacts(self, **kwargs) -> list:
        return []

    def create_draft(self, **kwargs) -> dict:
        raise NotImplementedError("Draft creation lands in a later phase")

    def send(self, **kwargs) -> dict:
        raise NotImplementedError("Sending stays hard-gated in later phases")

    def start_stub_sync(self, sync_type: str = "full") -> SyncRun:
        raise RuntimeError("Edge uses real sync, not stub sync")

    def mark_sync_complete(self) -> None:
        account = self._account()
        now = datetime.utcnow()
        account.last_sync_at = now
        account.last_sync_plain = plain_sync_label(now)
        account.status = "connected"
        account.updated_at = now
        self.db.commit()
