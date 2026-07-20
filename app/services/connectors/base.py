from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class AccountStatusView:
    account_id: str
    status: str
    connected: bool
    last_sync_at: datetime | None = None
    last_sync_plain: str | None = None
    permissions: dict[str, bool] = field(default_factory=dict)
    plain_message: str | None = None
    can_send: bool = False
    is_stub: bool = False
    partial_permissions: bool = False


@dataclass
class SyncPageResult:
    values: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None
    done: bool = True


class MailboxConnector(Protocol):
    account_id: str

    def status(self) -> AccountStatusView: ...

    def get_login_url(self) -> dict[str, Any]: ...

    def stub_connect(self) -> AccountStatusView: ...  # removed — real OAuth only

    def handle_oauth_callback(self, code: str) -> AccountStatusView: ...

    def disconnect(self) -> dict[str, Any]: ...

    def list_messages(self, folder: str = "sent", *, cursor: str | None = None) -> SyncPageResult: ...

    def get_thread(self, thread_id: str) -> dict[str, Any]: ...

    def list_events(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def get_contacts(self, **kwargs: Any) -> list[dict[str, Any]]: ...

    def create_draft(self, **kwargs: Any) -> dict[str, Any]: ...

    def send(self, **kwargs: Any) -> dict[str, Any]: ...

    def start_stub_sync(self, sync_type: str = "full") -> Any: ...
