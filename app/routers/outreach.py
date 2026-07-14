from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.outreach_intelligence_service import (
    MAX_BATCH_CONTACTS,
    OutreachIntelligenceError,
    analyze_contact_intelligence,
    create_batch_job,
    generate_personalized_draft,
    get_contact_intelligence,
    get_job,
    job_to_dict,
    run_batch_job,
)
from app.services.outreach_service import (
    OutreachError,
    draft_to_dict,
    generate_draft_for_contact,
    generate_drafts_bulk,
    get_prompt_config,
    list_drafts,
    send_approved_drafts,
    send_draft,
    update_draft,
    update_prompt_config,
)

router = APIRouter(prefix="/outreach", tags=["outreach"])


class PromptUpdate(BaseModel):
    system_prompt: str | None = None
    user_prompt_template: str | None = None


class GenerateDraftsRequest(BaseModel):
    contact_ids: list[str] = []
    custom_instructions: str | None = None
    personalized: bool = True
    target_use_case: str | None = None
    force_analyze: bool = False


class SingleGenerateRequest(BaseModel):
    custom_instructions: str | None = None
    personalized: bool = True
    target_use_case: str | None = None
    force_analyze: bool = False


class DraftUpdate(BaseModel):
    subject: str | None = None
    body: str | None = None
    status: str | None = None


class AnalyzeRequest(BaseModel):
    contact_ids: list[str] = Field(default_factory=list)
    force: bool = False
    generate_drafts: bool = False
    custom_instructions: str | None = None
    target_use_case: str | None = None
    async_batch: bool = True


class SingleAnalyzeRequest(BaseModel):
    force: bool = False
    target_use_case: str | None = None


@router.get("/prompt")
def get_prompt(db: Session = Depends(get_db)):
    return get_prompt_config(db)


@router.patch("/prompt")
def patch_prompt(payload: PromptUpdate, db: Session = Depends(get_db)):
    return update_prompt_config(
        db,
        system_prompt=payload.system_prompt,
        user_prompt_template=payload.user_prompt_template,
    )


@router.get("/drafts")
def get_drafts(status: str | None = None, db: Session = Depends(get_db)):
    drafts = list_drafts(db, status=status)
    return {"items": [draft_to_dict(d) for d in drafts]}


@router.post("/analyze")
async def post_analyze_batch(
    payload: AnalyzeRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Analyze email history, score contacts, optionally generate personalized drafts.

    For more than one contact (or when async_batch=true), runs as a background job
    suitable for testing ~50 contacts. Poll GET /outreach/jobs/{id} for progress.
    """
    ids = list(dict.fromkeys(payload.contact_ids))
    if not ids:
        raise HTTPException(status_code=400, detail="Select at least one contact")
    if len(ids) > MAX_BATCH_CONTACTS:
        raise HTTPException(status_code=400, detail=f"Batch limit is {MAX_BATCH_CONTACTS} contacts")

    use_async = payload.async_batch or len(ids) > 1 or payload.generate_drafts
    if use_async:
        try:
            job = create_batch_job(
                db,
                ids,
                generate_drafts=payload.generate_drafts,
                custom_instructions=payload.custom_instructions,
                target_use_case=payload.target_use_case,
                force=payload.force,
            )
        except OutreachIntelligenceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        background_tasks.add_task(run_batch_job, job.id)
        return {"job": job_to_dict(job)}

    try:
        intel = await analyze_contact_intelligence(
            db,
            ids[0],
            force=payload.force,
            target_use_case=payload.target_use_case,
        )
        return {"items": [intel]}
    except OutreachIntelligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/contacts/{contact_id}/analyze")
async def post_analyze_contact(
    contact_id: str,
    payload: SingleAnalyzeRequest | None = None,
    db: Session = Depends(get_db),
):
    force = payload.force if payload else False
    target = payload.target_use_case if payload else None
    try:
        return await analyze_contact_intelligence(
            db, contact_id, force=force, target_use_case=target
        )
    except OutreachIntelligenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Contact not found") from exc
        raise


@router.get("/contacts/{contact_id}/intelligence")
def get_intelligence(contact_id: str, db: Session = Depends(get_db)):
    try:
        result = get_contact_intelligence(db, contact_id)
    except Exception as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Contact not found") from exc
        raise
    if not result:
        raise HTTPException(status_code=404, detail="No outreach intelligence yet — run analyze first")
    return result


@router.get("/jobs/{job_id}")
def get_analyze_job(job_id: str, db: Session = Depends(get_db)):
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_to_dict(job)


@router.post("/drafts/generate")
async def post_generate_drafts(payload: GenerateDraftsRequest, db: Session = Depends(get_db)):
    if not payload.contact_ids:
        raise HTTPException(status_code=400, detail="Select at least one contact")

    if payload.personalized:
        # Route multi-contact personalized generation through the batch job API pattern
        # for a single contact we do it inline; for many, caller should use /analyze
        if len(payload.contact_ids) == 1:
            try:
                draft, intel = await generate_personalized_draft(
                    db,
                    payload.contact_ids[0],
                    custom_instructions=payload.custom_instructions,
                    target_use_case=payload.target_use_case,
                    force_analyze=payload.force_analyze,
                )
                return {"items": [draft_to_dict(draft)], "intelligence": [intel]}
            except OutreachIntelligenceError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        items = []
        results = []
        intel_list = []
        for contact_id in payload.contact_ids[:MAX_BATCH_CONTACTS]:
            try:
                draft, intel = await generate_personalized_draft(
                    db,
                    contact_id,
                    custom_instructions=payload.custom_instructions,
                    target_use_case=payload.target_use_case,
                    force_analyze=payload.force_analyze,
                )
                items.append(draft_to_dict(draft))
                intel_list.append(intel)
                results.append({"contact_id": contact_id, "draft_id": draft.id, "status": "ok"})
            except Exception as exc:
                results.append({"contact_id": contact_id, "status": "error", "error": str(exc)})
        return {"results": results, "items": items, "intelligence": intel_list}

    if len(payload.contact_ids) == 1:
        try:
            draft = await generate_draft_for_contact(
                db, payload.contact_ids[0], custom_instructions=payload.custom_instructions
            )
            return {"items": [draft_to_dict(draft)]}
        except OutreachError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    results = await generate_drafts_bulk(db, payload.contact_ids, custom_instructions=payload.custom_instructions)
    draft_ids = [r["draft_id"] for r in results if r.get("draft_id")]
    drafts = list_drafts(db)
    by_id = {d.id: d for d in drafts}
    items = [draft_to_dict(by_id[did]) for did in draft_ids if did in by_id]
    return {"results": results, "items": items}


@router.post("/contacts/{contact_id}/generate")
async def post_generate_for_contact(
    contact_id: str,
    payload: SingleGenerateRequest | None = None,
    db: Session = Depends(get_db),
):
    instructions = payload.custom_instructions if payload else None
    personalized = payload.personalized if payload else True
    target = payload.target_use_case if payload else None
    force = payload.force_analyze if payload else False
    try:
        if personalized:
            draft, intel = await generate_personalized_draft(
                db,
                contact_id,
                custom_instructions=instructions,
                target_use_case=target,
                force_analyze=force,
            )
            return {**draft_to_dict(draft), "intelligence": intel}
        draft = await generate_draft_for_contact(db, contact_id, custom_instructions=instructions)
        return draft_to_dict(draft)
    except (OutreachError, OutreachIntelligenceError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/drafts/{draft_id}")
def patch_draft(draft_id: str, payload: DraftUpdate, db: Session = Depends(get_db)):
    try:
        draft = update_draft(db, draft_id, subject=payload.subject, body=payload.body, status=payload.status)
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/approve")
def approve_draft(draft_id: str, db: Session = Depends(get_db)):
    try:
        draft = update_draft(db, draft_id, subject=None, body=None, status="approved")
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/drafts/{draft_id}/send")
async def post_send_draft(draft_id: str, db: Session = Depends(get_db)):
    try:
        draft = await send_draft(db, draft_id)
        return draft_to_dict(draft)
    except OutreachError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/drafts/send-approved")
async def post_send_approved(db: Session = Depends(get_db)):
    return {"results": await send_approved_drafts(db)}
