from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.message import EmailMessage
from app.models.sync import AuthToken
from app.services.outlook_link import resolve_outlook_url

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/{message_id}/open-outlook")
async def open_outlook(message_id: str, db: Session = Depends(get_db)):
    message = db.query(EmailMessage).filter(EmailMessage.id == message_id).one_or_none()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    url = await resolve_outlook_url(db, message)
    return RedirectResponse(url, status_code=302)
