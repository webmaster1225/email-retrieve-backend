from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models.account import MailboxAccount
from app.models.sync import SyncRun
from app.schemas import SyncRunOut
from app.services.connectors import get_connector, list_enabled_account_ids
from app.services.connectors.gmail_connector import GmailMailboxConnector
from app.services.connectors.graph_connector import GraphMailboxConnector
from app.services.gmail_client import GmailAuthError
from app.services.graph_client import GraphAuthError
from app.services.sync_service import SyncService, run_sync_in_background

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str
    email: str
    provider: str
    blurb: str
    status: str
    last_sync_at: datetime | None = None
    last_sync_plain: str | None = None
    permissions: dict[str, bool] = Field(default_factory=dict)
    is_functional: bool = False
    default_included: bool = True
    enabled: bool = True
    is_stub: bool = False
    connected: bool = False
    plain_message: str | None = None
    can_send: bool = False
    partial_permissions: bool = False


class DisconnectOut(BaseModel):
    connected: bool
    account_id: str
    consequences: str


def _db_factory():
    return SessionLocal()


def _to_out(account: MailboxAccount, status_view) -> AccountOut:
    return AccountOut(
        id=account.id,
        display_name=account.display_name,
        email=account.email,
        provider=account.provider,
        blurb=account.blurb,
        status=status_view.status,
        last_sync_at=status_view.last_sync_at,
        last_sync_plain=status_view.last_sync_plain,
        permissions=status_view.permissions or {},
        is_functional=account.is_functional,
        default_included=account.default_included,
        enabled=account.enabled,
        is_stub=False,
        connected=status_view.connected,
        plain_message=status_view.plain_message,
        can_send=status_view.can_send,
        partial_permissions=status_view.partial_permissions,
    )


def _require_accounts_ui() -> None:
    if not get_settings().feature_accounts_ui:
        raise HTTPException(status_code=404, detail="Accounts UI feature is disabled")


def _get_account_or_404(db: Session, account_id: str) -> MailboxAccount:
    if account_id not in list_enabled_account_ids():
        raise HTTPException(status_code=404, detail=f"Account not available: {account_id}")
    account = db.get(MailboxAccount, account_id)
    if not account or not account.enabled:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
    return account


@router.get("", response_model=list[AccountOut])
def list_accounts(db: Session = Depends(get_db)):
    _require_accounts_ui()
    results: list[AccountOut] = []
    for account_id in list_enabled_account_ids():
        account = db.get(MailboxAccount, account_id)
        if not account or not account.enabled:
            continue
        try:
            connector = get_connector(db, account_id)
            status_view = connector.status()
        except Exception:
            status_view = type(
                "S",
                (),
                {
                    "status": account.status,
                    "connected": False,
                    "last_sync_at": account.last_sync_at,
                    "last_sync_plain": account.last_sync_plain,
                    "permissions": account.permissions_json or {},
                    "is_stub": False,
                    "plain_message": account.status,
                    "can_send": False,
                    "partial_permissions": False,
                },
            )()
        results.append(_to_out(account, status_view))
    return results


@router.get("/{account_id}", response_model=AccountOut)
def get_account(account_id: str, db: Session = Depends(get_db)):
    _require_accounts_ui()
    account = _get_account_or_404(db, account_id)
    connector = get_connector(db, account_id)
    return _to_out(account, connector.status())


@router.get("/{account_id}/login")
def account_login(account_id: str, db: Session = Depends(get_db)):
    _require_accounts_ui()
    _get_account_or_404(db, account_id)
    connector = get_connector(db, account_id)
    info = connector.get_login_url()
    if info.get("error"):
        raise HTTPException(status_code=400, detail=info["error"])
    if not info.get("login_url"):
        raise HTTPException(status_code=400, detail="Could not build login URL")
    return info


@router.post("/{account_id}/stub-connect")
def stub_connect_removed(account_id: str):
    raise HTTPException(
        status_code=410,
        detail="Stub connect removed. Use Connect to sign in with Microsoft or Google.",
    )


@router.get("/{account_id}/oauth/callback")
async def account_oauth_callback(
    account_id: str,
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    _require_accounts_ui()
    settings = get_settings()
    _get_account_or_404(db, account_id)
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    connector = get_connector(db, account_id)
    try:
        if isinstance(connector, (GraphMailboxConnector, GmailMailboxConnector)):
            await connector.handle_oauth_callback_async(code)
        else:
            raise HTTPException(status_code=400, detail="Unsupported connector")
    except (GraphAuthError, GmailAuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"{settings.frontend_url.rstrip('/')}/settings?connected={account_id}")


@router.get("/{account_id}/status", response_model=AccountOut)
def account_status(account_id: str, db: Session = Depends(get_db)):
    _require_accounts_ui()
    account = _get_account_or_404(db, account_id)
    connector = get_connector(db, account_id)
    return _to_out(account, connector.status())


@router.post("/{account_id}/disconnect", response_model=DisconnectOut)
def disconnect_account(account_id: str, db: Session = Depends(get_db)):
    _require_accounts_ui()
    _get_account_or_404(db, account_id)
    connector = get_connector(db, account_id)
    result = connector.disconnect()
    return DisconnectOut(**result)


@router.post("/{account_id}/sync", response_model=SyncRunOut)
def start_account_sync(
    account_id: str,
    background_tasks: BackgroundTasks,
    sync_type: str = "full",
    db: Session = Depends(get_db),
):
    _require_accounts_ui()
    _get_account_or_404(db, account_id)
    connector = get_connector(db, account_id)

    # Graph accounts (Edge, Galaxy, Careers): real Sent Items sync
    if isinstance(connector, GraphMailboxConnector):
        try:
            connector.graph.ensure_access_token()
        except GraphAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        service = SyncService(db, account_id=account_id)
        active = service.get_active_run()
        if active:
            return active
        if sync_type == "inbox":
            sync_run = service.start_inbox_sync()
        else:
            sync_run = service.start_full_sync()
        background_tasks.add_task(run_sync_in_background, _db_factory, sync_run.id)
        return sync_run

    # Gmail: verify token and refresh last_sync (full message import later)
    if isinstance(connector, GmailMailboxConnector):
        try:
            connector.gmail.ensure_access_token()
        except GmailAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        run = SyncRun(account_id=account_id, sync_type=sync_type, status="running")
        db.add(run)
        db.commit()
        db.refresh(run)

        def _finish_gmail(run_id: str) -> None:
            db2 = SessionLocal()
            try:
                from datetime import datetime as dt

                from app.services.connectors.graph_connector import plain_sync_label

                r = db2.query(SyncRun).filter(SyncRun.id == run_id).one()
                now = dt.utcnow()
                r.status = "completed"
                r.completed_at = now
                acct = db2.get(MailboxAccount, account_id)
                if acct:
                    acct.last_sync_at = now
                    acct.last_sync_plain = plain_sync_label(now)
                    acct.status = "connected"
                db2.commit()
            finally:
                db2.close()

        background_tasks.add_task(_finish_gmail, run.id)
        return run

    raise HTTPException(status_code=400, detail="Sync not available for this account")


@router.get("/{account_id}/sync/status", response_model=SyncRunOut | None)
def account_sync_status(account_id: str, db: Session = Depends(get_db)):
    _require_accounts_ui()
    _get_account_or_404(db, account_id)
    return (
        db.query(SyncRun)
        .filter(SyncRun.account_id == account_id)
        .order_by(SyncRun.started_at.desc())
        .first()
    )
