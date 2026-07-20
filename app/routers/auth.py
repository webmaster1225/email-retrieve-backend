from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.sync import AuthToken
from app.schemas import AuthStatus
from app.services.graph_client import GraphAuthError, GraphClient, parse_token_scopes

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login")
def login(db: Session = Depends(get_db)):
    client = GraphClient(db, account_id="edge")
    if not client.settings.azure_client_id or not client.settings.azure_client_secret:
        raise HTTPException(
            status_code=500,
            detail="Azure credentials missing. Set AZURE_CLIENT_ID and AZURE_CLIENT_SECRET in .env",
        )
    try:
        return RedirectResponse(client.get_auth_url(state="account:edge"))
    except GraphAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/callback")
async def callback(
    code: str | None = None,
    error: str | None = None,
    admin_consent: str | None = None,
    db: Session = Depends(get_db),
):
    client = GraphClient(db, account_id="edge")
    if error:
        raise HTTPException(status_code=400, detail=f"Microsoft login failed: {error}")

    # Admin consent flow (Option A link) — no user auth code, consent only
    if admin_consent and admin_consent.lower() == "true":
        return RedirectResponse(f"{client.settings.frontend_url}?admin_consent=1")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        from app.models.account import MailboxAccount

        token_row = client.exchange_code(code)
        profile = await client.fetch_profile(token_row.access_token)
        token_row.user_email = profile.get("mail") or profile.get("userPrincipalName")
        token_row.user_id = profile.get("id")
        token_row.account_id = "edge"
        edge = db.get(MailboxAccount, "edge")
        if edge:
            edge.status = "connected"
            edge.permissions_json = {
                "read_mail": True,
                "send": True,
                "calendar": False,
                "drafts": True,
            }
        db.commit()
    except GraphAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(f"{client.settings.frontend_url}?connected=1")


@router.get("/status", response_model=AuthStatus)
def auth_status(db: Session = Depends(get_db)):
    client = GraphClient(db, account_id="edge")
    token_row = client.get_token_row()
    if not token_row:
        return AuthStatus(connected=False)

    try:
        access_token = client.ensure_access_token()
        connected = True
        scopes = parse_token_scopes(access_token)
    except GraphAuthError:
        connected = False
        scopes = []
    return AuthStatus(
        connected=connected,
        user_email=token_row.user_email,
        expires_at=token_row.expires_at,
        can_send_mail="Mail.Send" in scopes,
        token_scopes=scopes,
    )


@router.post("/disconnect")
def disconnect(db: Session = Depends(get_db)):
    """Legacy disconnect — only clears the Edge account token."""
    from app.models.account import MailboxAccount
    from app.services.connectors import get_connector

    try:
        connector = get_connector(db, "edge")
        return connector.disconnect()
    except Exception:
        db.query(AuthToken).filter(
            (AuthToken.account_id == "edge")
            | (AuthToken.account_id.is_(None))
            | (AuthToken.account_id == "")
        ).delete(synchronize_session=False)
        edge = db.get(MailboxAccount, "edge")
        if edge:
            edge.status = "not_connected"
            edge.permissions_json = {}
        db.commit()
        return {"connected": False, "account_id": "edge", "consequences": "Disconnected Edge Investing."}
