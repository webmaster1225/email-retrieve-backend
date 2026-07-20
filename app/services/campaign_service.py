"""Campaign orchestration for Compass P3–P5."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.campaign import (
    AuditEvent,
    Campaign,
    CampaignCandidate,
    CampaignDecision,
    PlanVersion,
)
from app.services.objective_parser import (
    apply_defaults_skip,
    parse_objective,
    parse_objective_heuristic,
)
from app.services.search_planner import apply_plan_revision, build_plan


def audit(
    db: Session,
    campaign_id: str,
    event_type: str,
    narrative: str,
    payload: dict | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        campaign_id=campaign_id,
        event_type=event_type,
        narrative=narrative,
        payload=payload or {},
    )
    db.add(ev)
    return ev


async def create_campaign(
    db: Session,
    *,
    objective: str,
    account_ids: list[str] | None = None,
) -> Campaign:
    parsed = await parse_objective(objective, round_num=0)
    accounts = list(account_ids or [])
    if not accounts:
        accounts = list(parsed.get("recommended_accounts") or ["edge", "northwyn"])
    # Careers never silently included
    if "careers" not in (account_ids or []):
        accounts = [a for a in accounts if a != "careers"]

    title = (parsed.get("beneficiary_entity") or objective[:60]).strip() or "Campaign"
    has_questions = bool(parsed.get("clarifying_questions"))
    status = "clarifying" if has_questions else "plan_pending"

    campaign = Campaign(
        title=title,
        objective_raw=objective.strip(),
        objective_parsed=parsed,
        status=status,
        account_ids=accounts,
        clarification_round=0,
        research_status="idle",
    )
    db.add(campaign)
    db.flush()

    plan = build_plan(
        objective_raw=campaign.objective_raw,
        parsed=parsed,
        account_ids=accounts,
    )
    pv = PlanVersion(
        campaign_id=campaign.id,
        version=1,
        plan_json=plan,
        assumptions=list(plan.get("assumptions") or []),
    )
    db.add(pv)
    db.flush()
    campaign.current_plan_version_id = pv.id

    audit(
        db,
        campaign.id,
        "objective_created",
        f"Objective captured: {campaign.objective_raw[:120]}",
        {"parsed": parsed, "account_ids": accounts},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


async def clarify_campaign(
    db: Session,
    campaign: Campaign,
    *,
    answer: str | None = None,
    use_defaults: bool = False,
) -> Campaign:
    parsed = dict(campaign.objective_parsed or {})
    round_num = (campaign.clarification_round or 0) + 1

    if use_defaults:
        parsed = apply_defaults_skip(parsed)
        campaign.objective_parsed = parsed
        campaign.clarification_round = round_num
        campaign.status = "plan_pending"
        # Rebuild plan with current accounts
        plan = build_plan(
            objective_raw=campaign.objective_raw,
            parsed=parsed,
            account_ids=list(campaign.account_ids or []),
        )
        _new_plan_version(db, campaign, plan, revision_note="Accepted defaults")
        audit(
            db,
            campaign.id,
            "clarification_skipped",
            "User accepted defaults",
            {"round": round_num},
        )
        db.commit()
        db.refresh(campaign)
        return campaign

    answers = []
    if answer and answer.strip():
        answers.append(answer.strip())
        assumptions = list(parsed.get("assumptions") or [])
        assumptions.append(f"Clarification: {answer.strip()}")
        parsed["assumptions"] = assumptions

    # Re-parse with prior context
    combined = campaign.objective_raw
    if answers:
        combined = campaign.objective_raw + "\n\nAdditional context: " + " ".join(answers)

    fresh = await parse_objective(
        combined, round_num=round_num, prior_answers=answers
    )
    # Preserve recommended accounts unless user named others
    if campaign.account_ids:
        fresh["recommended_accounts"] = list(campaign.account_ids)
    campaign.objective_parsed = fresh
    campaign.clarification_round = round_num

    if fresh.get("clarifying_questions") and round_num < 3:
        campaign.status = "clarifying"
    else:
        campaign.status = "plan_pending"
        fresh["clarifying_questions"] = []
        campaign.objective_parsed = fresh

    plan = build_plan(
        objective_raw=campaign.objective_raw,
        parsed=fresh,
        account_ids=list(campaign.account_ids or []),
    )
    _new_plan_version(
        db,
        campaign,
        plan,
        revision_note=answer.strip() if answer else None,
    )
    audit(
        db,
        campaign.id,
        "clarification",
        answer or "Clarification turn",
        {"round": round_num, "parsed": fresh},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def _new_plan_version(
    db: Session,
    campaign: Campaign,
    plan: dict[str, Any],
    *,
    revision_note: str | None = None,
) -> PlanVersion:
    version = 1
    if campaign.current_plan_version_id:
        cur = db.get(PlanVersion, campaign.current_plan_version_id)
        if cur:
            version = (cur.version or 1) + 1
    pv = PlanVersion(
        campaign_id=campaign.id,
        version=version,
        plan_json=plan,
        assumptions=list(plan.get("assumptions") or []),
        revision_note=revision_note,
        approved_at=None,
        approved_by=None,
    )
    db.add(pv)
    db.flush()
    campaign.current_plan_version_id = pv.id
    # Sync account ids from plan
    if plan.get("account_ids"):
        campaign.account_ids = list(plan["account_ids"])
    return pv


def revise_plan(db: Session, campaign: Campaign, instruction: str) -> Campaign:
    cur = (
        db.get(PlanVersion, campaign.current_plan_version_id)
        if campaign.current_plan_version_id
        else None
    )
    base = dict(cur.plan_json or {}) if cur else build_plan(
        objective_raw=campaign.objective_raw,
        parsed=dict(campaign.objective_parsed or {}),
        account_ids=list(campaign.account_ids or []),
    )
    revised = apply_plan_revision(base, instruction)
    _new_plan_version(db, campaign, revised, revision_note=instruction)
    campaign.status = "plan_pending"
    audit(
        db,
        campaign.id,
        "plan_revised",
        f"Plan revised: {instruction[:160]}",
        {"plan": revised},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def approve_plan(db: Session, campaign: Campaign, *, approved_by: str = "user") -> Campaign:
    if not campaign.current_plan_version_id:
        raise ValueError("No plan to approve")
    pv = db.get(PlanVersion, campaign.current_plan_version_id)
    if not pv:
        raise ValueError("Plan version missing")
    pv.approved_at = datetime.utcnow()
    pv.approved_by = approved_by
    campaign.status = "plan_pending"  # flips to researching on start
    audit(
        db,
        campaign.id,
        "plan_approved",
        "Gate 1: search plan approved",
        {"plan_version_id": pv.id, "version": pv.version},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def record_decisions(
    db: Session,
    campaign: Campaign,
    items: list[dict[str, str]],
    *,
    instruction_text: str | None = None,
) -> list[CampaignCandidate]:
    updated: list[CampaignCandidate] = []
    for item in items:
        cid = item.get("candidate_id")
        decision = item.get("decision")
        if not cid or decision not in ("include", "pass", "unsure"):
            continue
        row = db.get(CampaignCandidate, cid)
        if not row or row.campaign_id != campaign.id:
            continue
        row.decision = decision
        row.updated_at = datetime.utcnow()
        db.add(
            CampaignDecision(
                campaign_id=campaign.id,
                candidate_id=cid,
                decision=decision,
                instruction_text=instruction_text,
            )
        )
        updated.append(row)
    if updated:
        audit(
            db,
            campaign.id,
            "gate2_decisions",
            f"Gate 2: recorded {len(updated)} decision(s)",
            {
                "decisions": [
                    {"candidate_id": u.id, "decision": u.decision} for u in updated
                ],
                "instruction": instruction_text,
            },
        )
    db.commit()
    return updated


def campaign_to_dict(db: Session, campaign: Campaign) -> dict[str, Any]:
    plan = None
    plan_approved = False
    if campaign.current_plan_version_id:
        pv = db.get(PlanVersion, campaign.current_plan_version_id)
        if pv:
            plan = {
                "id": pv.id,
                "version": pv.version,
                "plan": pv.plan_json,
                "assumptions": pv.assumptions,
                "revision_note": pv.revision_note,
                "approved_at": pv.approved_at.isoformat() if pv.approved_at else None,
                "approved_by": pv.approved_by,
            }
            plan_approved = bool(pv.approved_at)

    candidates_count = (
        db.query(CampaignCandidate)
        .filter(CampaignCandidate.campaign_id == campaign.id)
        .count()
    )
    included = (
        db.query(CampaignCandidate)
        .filter(
            CampaignCandidate.campaign_id == campaign.id,
            CampaignCandidate.decision == "include",
        )
        .count()
    )
    return {
        "id": campaign.id,
        "title": campaign.title,
        "objective_raw": campaign.objective_raw,
        "objective_parsed": campaign.objective_parsed,
        "status": campaign.status,
        "account_ids": campaign.account_ids or [],
        "clarification_round": campaign.clarification_round,
        "research_status": campaign.research_status,
        "research_progress": campaign.research_progress,
        "research_error": campaign.research_error,
        "research_mode": campaign.research_mode or "relationship_only",
        "message_strategy": campaign.message_strategy or {},
        "external_research_status": campaign.external_research_status,
        "external_research_progress": campaign.external_research_progress,
        "sending_account_id": campaign.sending_account_id,
        "sending_account_confirmed_at": (
            campaign.sending_account_confirmed_at.isoformat()
            if campaign.sending_account_confirmed_at
            else None
        ),
        "plan": plan,
        "plan_approved": plan_approved,
        "candidates_count": candidates_count,
        "included_count": included,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
    }


def candidate_to_dict(c: CampaignCandidate) -> dict[str, Any]:
    return {
        "id": c.id,
        "contact_id": c.contact_id,
        "rank": c.rank,
        "full_name": c.full_name,
        "email": c.email,
        "company": c.company,
        "role_label": c.role_label,
        "strength_label": c.strength_label,
        "relevance_label": c.relevance_label,
        "why_text": c.why_text,
        "source_accounts": c.source_accounts or [],
        "decision": c.decision,
        "flags": c.flags or [],
        "evidence": [
            {
                "id": e.id,
                "kind": e.kind,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                "source_account": e.source_account,
                "direction": e.direction,
                "subject": e.subject,
                "summary": e.summary,
                "message_id": e.message_id,
                "outlook_weblink": e.outlook_weblink,
                "citation_ok": e.citation_ok,
            }
            for e in (c.evidence_items or [])
        ],
    }


# Re-export heuristic for tests
__all__ = [
    "audit",
    "approve_plan",
    "campaign_to_dict",
    "candidate_to_dict",
    "clarify_campaign",
    "create_campaign",
    "parse_objective_heuristic",
    "record_decisions",
    "revise_plan",
]
