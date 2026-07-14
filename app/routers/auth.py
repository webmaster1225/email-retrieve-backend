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
    client = GraphClient(db)
    if not client.settings.azure_client_id or not client.settings.azure_client_secret:
        raise HTTPException(
            status_code=500,
            detail="Azure credentials missing. Set AZURE_CLIENT_ID and AZURE_CLIENT_SECRET in .env",
        )
    try:
        return RedirectResponse(client.get_auth_url(state="crm"))
    except GraphAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/callback")
async def callback(
    code: str | None = None,
    error: str | None = None,
    admin_consent: str | None = None,
    db: Session = Depends(get_db),
):
    client = GraphClient(db)
    if error:
        raise HTTPException(status_code=400, detail=f"Microsoft login failed: {error}")

    # Admin consent flow (Option A link) — no user auth code, consent only
    if admin_consent and admin_consent.lower() == "true":
        return RedirectResponse(f"{client.settings.frontend_url}?admin_consent=1")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        token_row = client.exchange_code(code)
        profile = await client.fetch_profile(token_row.access_token)
        token_row.user_email = profile.get("mail") or profile.get("userPrincipalName")
        token_row.user_id = profile.get("id")
        db.commit()
    except GraphAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(f"{client.settings.frontend_url}?connected=1")


@router.get("/status", response_model=AuthStatus)
def auth_status(db: Session = Depends(get_db)):
    token_row = db.query(AuthToken).order_by(AuthToken.updated_at.desc()).first()
    if not token_row:
        return AuthStatus(connected=False)

    client = GraphClient(db)
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
    db.query(AuthToken).delete()
    db.commit()
    return {"connected": False}
