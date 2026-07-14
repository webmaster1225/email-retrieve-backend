from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.ai_service import (
    AIServiceError,
    ai_status,
    analyze_relationship,
    analyze_relationship_for_outlook,
    classify_contact,
    detect_seniority,
    detect_seniority_for_outlook,
    generate_follow_up,
    generate_summary,
    summarize_threads,
)
from app.services.text_utils import normalize_email

router = APIRouter(prefix="/contacts", tags=["ai"])


class SeniorityOutlookContext(BaseModel):
    full_name: str | None = None
    company_name: str | None = None
    last_subject: str | None = None
    last_preview: str | None = None


RelationshipOutlookContext = SeniorityOutlookContext


@router.post("/by-email/{contact_email:path}/ai/seniority")
async def post_ai_seniority_by_email(
    contact_email: str,
    body: SeniorityOutlookContext | None = None,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    email = normalize_email(contact_email)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    ctx = body or SeniorityOutlookContext()
    try:
        return await detect_seniority_for_outlook(
            db,
            email,
            full_name=ctx.full_name,
            company_name=ctx.company_name,
            last_subject=ctx.last_subject,
            last_preview=ctx.last_preview,
            force=force,
        )
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/by-email/{contact_email:path}/ai/relationship")
async def post_ai_relationship_by_email(
    contact_email: str,
    body: RelationshipOutlookContext | None = None,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    email = normalize_email(contact_email)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid email address")
    ctx = body or RelationshipOutlookContext()
    try:
        return await analyze_relationship_for_outlook(
            db,
            email,
            full_name=ctx.full_name,
            company_name=ctx.company_name,
            last_subject=ctx.last_subject,
            last_preview=ctx.last_preview,
            force=force,
        )
    except (AIServiceError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{contact_id}/ai/status")
def get_ai_status(contact_id: str, db: Session = Depends(get_db)):
    try:
        return ai_status(db, contact_id)
    except AIServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/summary")
async def post_ai_summary(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await generate_summary(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/follow-up")
async def post_ai_follow_up(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await generate_follow_up(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/classify")
async def post_ai_classify(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await classify_contact(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/seniority")
async def post_ai_seniority(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await detect_seniority(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/relationship")
async def post_ai_relationship(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await analyze_relationship(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{contact_id}/ai/summarize-threads")
async def post_ai_summarize_threads(
    contact_id: str,
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    try:
        return await summarize_threads(db, contact_id, force=force)
    except AIServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
