"""Compass campaign API — P3 objective, P4 plan/research, P5 decisions."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models.campaign import AuditEvent, Campaign, CampaignCandidate
from app.services import campaign_service
from app.services.campaign_retrieval import Gate1NotApprovedError, run_campaign_research
from app.services.nl_list_ops import apply_nl_op, preview_nl_op

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _require_compass() -> None:
    if not get_settings().feature_compass_campaigns:
        raise HTTPException(
            status_code=404,
            detail="Compass campaigns disabled. Set FEATURE_COMPASS_CAMPAIGNS=true",
        )


def _get_campaign(db: Session, campaign_id: str) -> Campaign:
    row = db.get(Campaign, campaign_id)
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return row


class CreateCampaignIn(BaseModel):
    objective: str = Field(min_length=1)
    account_ids: list[str] = Field(default_factory=list)


class ClarifyIn(BaseModel):
    answer: str | None = None
    use_defaults: bool = False


class PlanReviseIn(BaseModel):
    instruction: str = Field(min_length=1)


class DecisionItem(BaseModel):
    candidate_id: str
    decision: str  # include|pass|unsure


class DecisionsIn(BaseModel):
    items: list[DecisionItem]
    instruction_text: str | None = None


class NlOpIn(BaseModel):
    instruction: str = Field(min_length=1)


class SuppressThreadIn(BaseModel):
    subject: str = Field(min_length=1)
    evidence_id: str | None = None
    rerun_research: bool = True


@router.get("")
def list_campaigns(db: Session = Depends(get_db), summary: bool = False):
    _require_compass()
    if summary:
        from app.services.campaign_tracking import list_campaigns_summary

        return list_campaigns_summary(db)
    rows = (
        db.query(Campaign)
        .filter(Campaign.status != "archived")
        .order_by(Campaign.updated_at.desc())
        .limit(50)
        .all()
    )
    return [campaign_service.campaign_to_dict(db, c) for c in rows]


@router.post("")
async def create_campaign(body: CreateCampaignIn, db: Session = Depends(get_db)):
    _require_compass()
    campaign = await campaign_service.create_campaign(
        db, objective=body.objective, account_ids=body.account_ids
    )
    return campaign_service.campaign_to_dict(db, campaign)


@router.get("/{campaign_id}")
def get_campaign(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    return campaign_service.campaign_to_dict(db, campaign)


@router.post("/{campaign_id}/clarify")
async def clarify(campaign_id: str, body: ClarifyIn, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    campaign = await campaign_service.clarify_campaign(
        db, campaign, answer=body.answer, use_defaults=body.use_defaults
    )
    return campaign_service.campaign_to_dict(db, campaign)


@router.post("/{campaign_id}/plan/revise")
def revise_plan(campaign_id: str, body: PlanReviseIn, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    campaign = campaign_service.revise_plan(db, campaign, body.instruction)
    return campaign_service.campaign_to_dict(db, campaign)


@router.post("/{campaign_id}/plan/approve")
def approve_plan(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    try:
        campaign = campaign_service.approve_plan(db, campaign)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return campaign_service.campaign_to_dict(db, campaign)


def _research_job(campaign_id: str) -> None:
    db = SessionLocal()
    try:
        run_campaign_research(db, campaign_id)
        campaign_service.audit(
            db,
            campaign_id,
            "research_completed",
            "Relationship research completed",
            {},
        )
        db.commit()
    except Gate1NotApprovedError as exc:
        campaign = db.get(Campaign, campaign_id)
        if campaign:
            campaign.research_status = "failed"
            campaign.research_error = str(exc)
            db.commit()
    except Exception as exc:
        campaign = db.get(Campaign, campaign_id)
        if campaign:
            campaign.research_status = "failed"
            campaign.research_error = str(exc)
            db.commit()
    finally:
        db.close()


@router.post("/{campaign_id}/research/start")
def start_research(
    campaign_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    try:
        from app.services.campaign_retrieval import require_approved_plan

        require_approved_plan(db, campaign)
    except Gate1NotApprovedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if campaign.research_status == "running":
        return {
            "status": "running",
            "progress": campaign.research_progress,
            "campaign": campaign_service.campaign_to_dict(db, campaign),
        }

    campaign.research_status = "running"
    campaign.research_progress = "Starting relationship research…"
    campaign.status = "researching"
    db.commit()
    background_tasks.add_task(_research_job, campaign_id)
    return {
        "status": "running",
        "progress": campaign.research_progress,
        "campaign": campaign_service.campaign_to_dict(db, campaign),
    }


@router.get("/{campaign_id}/research/status")
def research_status(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    return {
        "status": campaign.research_status or "idle",
        "progress": campaign.research_progress,
        "error": campaign.research_error,
        "campaign": campaign_service.campaign_to_dict(db, campaign),
    }


@router.get("/{campaign_id}/candidates")
def list_candidates(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    try:
        from app.services.campaign_retrieval import require_approved_plan

        require_approved_plan(db, campaign)
    except Gate1NotApprovedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    rows = (
        db.query(CampaignCandidate)
        .options(joinedload(CampaignCandidate.evidence_items))
        .filter(CampaignCandidate.campaign_id == campaign_id)
        .order_by(CampaignCandidate.rank.asc())
        .all()
    )
    return [campaign_service.candidate_to_dict(c) for c in rows]


@router.post("/{campaign_id}/decisions")
def set_decisions(campaign_id: str, body: DecisionsIn, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    updated = campaign_service.record_decisions(
        db,
        campaign,
        [i.model_dump() for i in body.items],
        instruction_text=body.instruction_text,
    )
    return {
        "updated": len(updated),
        "campaign": campaign_service.campaign_to_dict(db, campaign),
    }


@router.post("/{campaign_id}/nl-ops/preview")
def nl_preview(campaign_id: str, body: NlOpIn, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    preview = preview_nl_op(db, campaign_id, body.instruction)
    return {
        "instruction": preview.instruction,
        "restatement": preview.restatement,
        "candidate_ids": preview.candidate_ids,
        "action": preview.action,
        "matched_count": preview.matched_count,
    }


@router.post("/{campaign_id}/nl-ops/apply")
def nl_apply(campaign_id: str, body: NlOpIn, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    preview = preview_nl_op(db, campaign_id, body.instruction)
    result = apply_nl_op(db, campaign_id, preview)
    if preview.candidate_ids:
        for cid in preview.candidate_ids:
            from app.models.campaign import CampaignDecision

            db.add(
                CampaignDecision(
                    campaign_id=campaign_id,
                    candidate_id=cid,
                    decision=preview.action,
                    instruction_text=body.instruction,
                )
            )
    campaign_service.audit(
        db,
        campaign_id,
        "nl_list_op",
        preview.restatement,
        {"instruction": body.instruction, "result": result},
    )
    db.commit()
    return {**result, "campaign": campaign_service.campaign_to_dict(db, campaign)}


@router.post("/{campaign_id}/suppress-thread")
def suppress_thread(
    campaign_id: str,
    body: SuppressThreadIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """One-click 'ignore this thread' — seed suppression list and optionally re-research."""
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    from app.models.campaign import PlanVersion
    from app.services.conversation_suppression import add_suppression_to_plan

    plan_row = None
    if campaign.current_plan_version_id:
        plan_row = db.get(PlanVersion, campaign.current_plan_version_id)
    if not plan_row:
        raise HTTPException(status_code=409, detail="No plan to update with suppressions")

    plan = add_suppression_to_plan(dict(plan_row.plan_json or {}), body.subject)
    plan_row.plan_json = plan
    # Also keep on message_strategy so drafting/research share the list
    strategy = dict(campaign.message_strategy or {})
    strategy = add_suppression_to_plan(strategy, body.subject)
    campaign.message_strategy = strategy
    campaign_service.audit(
        db,
        campaign_id,
        "thread_suppressed",
        f"Ignoring thread: {body.subject[:120]}",
        {"subject": body.subject, "evidence_id": body.evidence_id},
    )
    db.commit()

    if body.rerun_research and plan_row.approved_at:
        # Recompute candidates/hooks/strength on cleaned set
        def _job(cid: str) -> None:
            session = SessionLocal()
            try:
                run_campaign_research(session, cid)
            finally:
                session.close()

        background_tasks.add_task(_job, campaign_id)

    return {
        "suppressed_subjects": plan.get("suppressed_subjects") or [],
        "research_restarted": bool(body.rerun_research and plan_row.approved_at),
        "campaign": campaign_service.campaign_to_dict(db, campaign),
    }


@router.get("/{campaign_id}/audit")
def list_audit(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    rows = (
        db.query(AuditEvent)
        .filter(AuditEvent.campaign_id == campaign_id)
        .order_by(AuditEvent.created_at.asc())
        .limit(200)
        .all()
    )
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "narrative": e.narrative,
            "payload": e.payload,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in rows
    ]


# ── P6–P9 ──────────────────────────────────────────────────────────────


class ConfirmIn(BaseModel):
    notes: str | None = None
    ask: str | None = None
    research_mode: str = "relationship_only"  # relationship_only|light|standard|enhanced


class FactDecisionItem(BaseModel):
    fact_id: str
    decision: str  # approved|background|rejected


class FactsDecisionsIn(BaseModel):
    items: list[FactDecisionItem]


class ToneIn(BaseModel):
    mode: str  # shorter|warmer|direct
    scope: str = "all"  # one|all
    draft_id: str | None = None


class DraftPatchIn(BaseModel):
    subject: str | None = None
    body: str | None = None


class SendingAccountIn(BaseModel):
    account_id: str
    careers_justification: str | None = None


class ScheduleIn(BaseModel):
    scheduled_for: str  # ISO datetime


class SendAuthorizeIn(BaseModel):
    confirm: bool = False
    recipient_emails: list[str] = Field(default_factory=list)


def _external_research_job(campaign_id: str) -> None:
    db = SessionLocal()
    try:
        from app.services.research import run_external_research

        run_external_research(db, campaign_id)
    except Exception as exc:
        campaign = db.get(Campaign, campaign_id)
        if campaign:
            campaign.external_research_status = "failed"
            campaign.external_research_progress = str(exc)
            db.commit()
    finally:
        db.close()


@router.post("/{campaign_id}/confirm")
def confirm_campaign(campaign_id: str, body: ConfirmIn, db: Session = Depends(get_db)):
    """Gate 2 complete → message strategy + research mode (Stage 6–7)."""
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    mode = (body.research_mode or get_settings().research_mode_default or "relationship_only").lower()
    if mode not in ("relationship_only", "light", "standard", "enhanced"):
        mode = "relationship_only"
    campaign.message_strategy = {
        "notes": body.notes or "",
        "ask": body.ask or "Would you have 15 minutes soon?",
    }
    campaign.research_mode = mode
    campaign.status = "confirming"
    campaign_service.audit(
        db,
        campaign_id,
        "campaign_confirmed",
        f"Strategy captured; research_mode={mode}",
        {"strategy": campaign.message_strategy, "research_mode": mode},
    )
    db.commit()
    db.refresh(campaign)
    return campaign_service.campaign_to_dict(db, campaign)


@router.post("/{campaign_id}/external-research/start")
def start_external_research(
    campaign_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    if campaign.external_research_status == "running":
        return {
            "status": "running",
            "progress": campaign.external_research_progress,
            "campaign": campaign_service.campaign_to_dict(db, campaign),
        }
    campaign.external_research_status = "running"
    campaign.external_research_progress = "Starting external research…"
    campaign.status = "external_research"
    db.commit()
    background_tasks.add_task(_external_research_job, campaign_id)
    return {
        "status": "running",
        "progress": campaign.external_research_progress,
        "campaign": campaign_service.campaign_to_dict(db, campaign),
    }


@router.get("/{campaign_id}/external-research/status")
def external_research_status(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    return {
        "status": campaign.external_research_status or "idle",
        "progress": campaign.external_research_progress,
        "campaign": campaign_service.campaign_to_dict(db, campaign),
    }


@router.get("/{campaign_id}/facts")
def list_facts(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.models.campaign import ExternalFact

    rows = (
        db.query(ExternalFact)
        .filter(ExternalFact.campaign_id == campaign_id)
        .order_by(ExternalFact.created_at.asc())
        .all()
    )
    # Gate 3 UI: show proposed + identity-confirmed; also show rejected identity notes
    out = []
    for f in rows:
        if f.status == "rejected" and not f.identity_confirmed:
            # Surface as honest identity-uncertain card (read-only style)
            pass
        out.append(
            {
                "id": f.id,
                "candidate_id": f.candidate_id,
                "claim": f.claim,
                "sources": f.sources or [],
                "publication_date": f.publication_date.isoformat() if f.publication_date else None,
                "event_date": f.event_date.isoformat() if f.event_date else None,
                "retrieved_at": f.retrieved_at.isoformat() if f.retrieved_at else None,
                "confidence": f.confidence,
                "status": f.status,
                "identity_confirmed": f.identity_confirmed,
                "quarantined_reason": f.quarantined_reason,
                "recommended_use": f.recommended_use,
            }
        )
    return out


@router.post("/{campaign_id}/facts/decisions")
def fact_decisions(campaign_id: str, body: FactsDecisionsIn, db: Session = Depends(get_db)):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    from app.models.campaign import ExternalFact

    updated = 0
    for item in body.items:
        if item.decision not in ("approved", "background", "rejected"):
            continue
        fact = db.get(ExternalFact, item.fact_id)
        if not fact or fact.campaign_id != campaign_id:
            continue
        if not fact.identity_confirmed and item.decision in ("approved", "background"):
            raise HTTPException(
                status_code=400,
                detail="Cannot approve a fact with unconfirmed identity",
            )
        fact.status = item.decision
        updated += 1
    campaign_service.audit(
        db,
        campaign_id,
        "gate3_fact_decisions",
        f"Gate 3: recorded {updated} fact decision(s)",
        {"items": [i.model_dump() for i in body.items]},
    )
    db.commit()
    return {"updated": updated, "campaign": campaign_service.campaign_to_dict(db, campaign)}


@router.post("/{campaign_id}/drafts/generate")
async def generate_drafts(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_drafting import draft_to_dict, generate_campaign_drafts

    try:
        drafts = await generate_campaign_drafts(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": [draft_to_dict(d) for d in drafts]}


@router.get("/{campaign_id}/drafts")
def list_drafts(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.models.campaign import CampaignDraft
    from app.services.campaign_drafting import draft_to_dict

    rows = (
        db.query(CampaignDraft)
        .filter(CampaignDraft.campaign_id == campaign_id)
        .order_by(CampaignDraft.created_at.asc())
        .all()
    )
    return [draft_to_dict(d) for d in rows]


@router.patch("/{campaign_id}/drafts/{draft_id}")
def patch_draft(
    campaign_id: str, draft_id: str, body: DraftPatchIn, db: Session = Depends(get_db)
):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.models.campaign import CampaignDraft
    from app.services.campaign_drafting import draft_to_dict, lint_banned_phrases

    draft = db.get(CampaignDraft, draft_id)
    if not draft or draft.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Draft not found")
    if body.subject is not None:
        draft.subject = body.subject
    if body.body is not None:
        draft.body = body.body
        draft.warnings = lint_banned_phrases(body.body)
    draft.status = "edited"
    campaign_service.audit(db, campaign_id, "draft_edited", f"Edited draft {draft_id}", {})
    db.commit()
    db.refresh(draft)
    return draft_to_dict(draft)


@router.post("/{campaign_id}/drafts/{draft_id}/approve")
def approve_draft(campaign_id: str, draft_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.models.campaign import CampaignDraft
    from app.services.campaign_drafting import draft_to_dict

    draft = db.get(CampaignDraft, draft_id)
    if not draft or draft.campaign_id != campaign_id:
        raise HTTPException(status_code=404, detail="Draft not found")
    draft.status = "approved"
    campaign_service.audit(db, campaign_id, "gate4_draft_approved", f"Approved draft {draft_id}", {})
    db.commit()
    db.refresh(draft)
    return draft_to_dict(draft)


@router.post("/{campaign_id}/drafts/apply-tone")
def apply_tone(campaign_id: str, body: ToneIn, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_drafting import apply_tone as do_tone
    from app.services.campaign_drafting import draft_to_dict

    rows = do_tone(
        db, campaign_id, mode=body.mode, scope=body.scope, draft_id=body.draft_id
    )
    return {"items": [draft_to_dict(d) for d in rows]}


@router.post("/{campaign_id}/sending-account")
def set_sending_account(
    campaign_id: str, body: SendingAccountIn, db: Session = Depends(get_db)
):
    _require_compass()
    campaign = _get_campaign(db, campaign_id)
    from app.services.campaign_send import SendGateError, confirm_sending_account

    try:
        campaign = confirm_sending_account(
            db,
            campaign,
            account_id=body.account_id,
            careers_justification=body.careers_justification,
        )
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return campaign_service.campaign_to_dict(db, campaign)


class DraftOverrideIn(BaseModel):
    account_id: str | None = None


@router.post("/{campaign_id}/drafts/{draft_id}/sending-account")
def set_draft_sending_account(
    campaign_id: str,
    draft_id: str,
    body: DraftOverrideIn,
    db: Session = Depends(get_db),
):
    """Per-recipient Gate 5 override (G-10)."""
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_send import SendGateError, set_draft_sending_override
    from app.services.campaign_drafting import draft_to_dict

    try:
        draft = set_draft_sending_override(
            db, campaign_id, draft_id=draft_id, account_id=body.account_id
        )
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return draft_to_dict(draft)


@router.get("/{campaign_id}/preflight")
def campaign_preflight(campaign_id: str, db: Session = Depends(get_db)):
    """Stage 11 attention list before Save / Schedule / Send."""
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_send import SendGateError, build_preflight

    try:
        return build_preflight(db, campaign_id)
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{campaign_id}/drafts/save-to-mailbox")
async def save_to_mailbox(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_send import SendGateError, save_drafts_to_mailbox

    try:
        results = await save_drafts_to_mailbox(db, campaign_id)
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"results": results}


@router.post("/{campaign_id}/schedule")
def schedule(campaign_id: str, body: ScheduleIn, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from datetime import datetime

    from app.services.campaign_send import SendGateError, schedule_sends

    try:
        when = datetime.fromisoformat(body.scheduled_for.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
        logs = schedule_sends(db, campaign_id, scheduled_for=when)
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid scheduled_for: {exc}") from exc
    return {
        "items": [
            {
                "id": log.id,
                "action": log.action,
                "recipient": log.recipient,
                "scheduled_for": log.scheduled_for.isoformat() if log.scheduled_for else None,
            }
            for log in logs
        ]
    }


@router.delete("/{campaign_id}/schedule/{send_log_id}")
def cancel_scheduled(campaign_id: str, send_log_id: str, db: Session = Depends(get_db)):
    _require_compass()
    from app.services.campaign_send import SendGateError, cancel_schedule

    try:
        log = cancel_schedule(db, campaign_id, send_log_id)
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"id": log.id, "action": log.action}


@router.post("/{campaign_id}/send/preview")
def send_preview_endpoint(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_send import SendGateError, send_preview

    try:
        return send_preview(db, campaign_id)
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{campaign_id}/send")
async def send_authorize(
    campaign_id: str, body: SendAuthorizeIn, db: Session = Depends(get_db)
):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_send import SendGateError, authorize_send

    try:
        results = await authorize_send(
            db,
            campaign_id,
            confirm=body.confirm,
            recipient_emails=body.recipient_emails,
        )
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"results": results}


@router.get("/{campaign_id}/send-log")
def get_send_log(campaign_id: str, db: Session = Depends(get_db)):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.models.campaign import SendLog

    rows = (
        db.query(SendLog)
        .filter(SendLog.campaign_id == campaign_id)
        .order_by(SendLog.created_at.asc())
        .all()
    )
    return [
        {
            "id": r.id,
            "draft_id": r.draft_id,
            "recipient": r.recipient,
            "account_id": r.account_id,
            "action": r.action,
            "subject": r.subject,
            "body_hash": r.body_hash,
            "conversation_id": r.conversation_id,
            "internet_message_id": r.internet_message_id,
            "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
            "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            "error": r.error,
            "authorized_at": r.authorized_at.isoformat() if r.authorized_at else None,
        }
        for r in rows
    ]


# --- P7 draft verbs ---


class ChangeAskIn(BaseModel):
    ask: str = Field(min_length=1)


class VariantIn(BaseModel):
    variant: str = Field(min_length=1)


@router.post("/{campaign_id}/drafts/{draft_id}/regenerate")
async def regenerate_draft_endpoint(
    campaign_id: str, draft_id: str, body: VariantIn | None = None, db: Session = Depends(get_db)
):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_drafting import draft_to_dict, regenerate_draft

    try:
        draft = await regenerate_draft(
            db, campaign_id, draft_id, variant=(body.variant if body else None)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return draft_to_dict(draft)


@router.post("/{campaign_id}/drafts/{draft_id}/variant")
async def set_variant(
    campaign_id: str, draft_id: str, body: VariantIn, db: Session = Depends(get_db)
):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_drafting import draft_to_dict, set_draft_variant

    try:
        draft = await set_draft_variant(db, campaign_id, draft_id, body.variant)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return draft_to_dict(draft)


@router.post("/{campaign_id}/drafts/{draft_id}/change-ask")
def change_ask_endpoint(
    campaign_id: str, draft_id: str, body: ChangeAskIn, db: Session = Depends(get_db)
):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_drafting import change_ask, draft_to_dict

    try:
        draft = change_ask(db, campaign_id, draft_id, body.ask)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return draft_to_dict(draft)


@router.post("/{campaign_id}/drafts/{draft_id}/remove-public-refs")
def remove_public_refs_endpoint(
    campaign_id: str, draft_id: str, db: Session = Depends(get_db)
):
    _require_compass()
    _get_campaign(db, campaign_id)
    from app.services.campaign_drafting import draft_to_dict, remove_public_refs

    try:
        draft = remove_public_refs(db, campaign_id, draft_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return draft_to_dict(draft)


# --- P10 tracking ---


def _require_tracking() -> None:
    _require_compass()
    if not get_settings().feature_compass_tracking:
        raise HTTPException(
            status_code=404,
            detail="Campaign tracking disabled. Set FEATURE_COMPASS_TRACKING=true",
        )


@router.get("/{campaign_id}/tracking")
def get_tracking(campaign_id: str, db: Session = Depends(get_db)):
    _require_tracking()
    _get_campaign(db, campaign_id)
    from app.services.campaign_tracking import tracking_dashboard

    try:
        return tracking_dashboard(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{campaign_id}/tracking/refresh")
def refresh_tracking(campaign_id: str, db: Session = Depends(get_db)):
    _require_tracking()
    _get_campaign(db, campaign_id)
    from app.services.campaign_tracking import refresh_campaign_tracking

    try:
        return refresh_campaign_tracking(db, campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- P11 follow-ups ---


def _require_followups() -> None:
    _require_compass()
    if not get_settings().feature_compass_followups:
        raise HTTPException(
            status_code=404,
            detail="Follow-ups disabled. Set FEATURE_COMPASS_FOLLOWUPS=true",
        )


class FollowUpSendIn(BaseModel):
    confirm: bool = False
    recipient_email: str | None = None


@router.post("/{campaign_id}/follow-ups/propose")
def propose_followups_endpoint(campaign_id: str, db: Session = Depends(get_db)):
    _require_followups()
    _get_campaign(db, campaign_id)
    from app.services.campaign_followups import propose_followups, _proposal_to_dict
    from app.services.campaign_send import SendGateError

    try:
        rows = propose_followups(db, campaign_id)
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"items": [_proposal_to_dict(r) for r in rows]}


@router.get("/{campaign_id}/follow-ups")
def list_followups_endpoint(campaign_id: str, db: Session = Depends(get_db)):
    _require_followups()
    _get_campaign(db, campaign_id)
    from app.services.campaign_followups import list_followups

    return list_followups(db, campaign_id)


@router.post("/{campaign_id}/follow-ups/{followup_id}/approve")
def approve_followup(campaign_id: str, followup_id: str, db: Session = Depends(get_db)):
    _require_followups()
    from app.services.campaign_followups import _proposal_to_dict, set_followup_status
    from app.services.campaign_send import SendGateError

    try:
        row = set_followup_status(db, campaign_id, followup_id, "approved")
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _proposal_to_dict(row)


@router.post("/{campaign_id}/follow-ups/{followup_id}/reject")
def reject_followup(campaign_id: str, followup_id: str, db: Session = Depends(get_db)):
    _require_followups()
    from app.services.campaign_followups import _proposal_to_dict, set_followup_status
    from app.services.campaign_send import SendGateError

    try:
        row = set_followup_status(db, campaign_id, followup_id, "rejected")
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _proposal_to_dict(row)


@router.post("/{campaign_id}/follow-ups/{followup_id}/send")
async def send_followup(
    campaign_id: str,
    followup_id: str,
    body: FollowUpSendIn,
    db: Session = Depends(get_db),
):
    _require_followups()
    from app.services.campaign_followups import authorize_followup_send
    from app.services.campaign_send import SendGateError

    try:
        return await authorize_followup_send(
            db,
            campaign_id,
            followup_id,
            confirm=body.confirm,
            recipient_email=body.recipient_email,
        )
    except SendGateError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
