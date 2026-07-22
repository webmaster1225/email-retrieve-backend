"""P11 — follow-up proposals and Gate 9 authorize send."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.campaign import (
    Campaign,
    CampaignCandidate,
    CampaignCommitment,
    CampaignDraft,
    FollowUpProposal,
    SendLog,
)
from app.services.campaign_drafting import body_hash
from app.services.campaign_send import SendGateError, require_sending_account
from app.services.campaign_service import audit

logger = logging.getLogger(__name__)


def _proposal_to_dict(p: FollowUpProposal) -> dict[str, Any]:
    return {
        "id": p.id,
        "campaign_id": p.campaign_id,
        "candidate_id": p.candidate_id,
        "kind": p.kind,
        "subject": p.subject,
        "body": p.body,
        "status": p.status,
        "based_on_status": p.based_on_status,
        "gate9_authorized_at": p.gate9_authorized_at.isoformat()
        if p.gate9_authorized_at
        else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def propose_followups(db: Session, campaign_id: str) -> list[FollowUpProposal]:
    settings = get_settings()
    if not settings.feature_compass_followups:
        raise SendGateError("FEATURE_COMPASS_FOLLOWUPS is disabled", 403)

    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise SendGateError("Campaign not found", 404)

    # Ensure aged "sent" contacts are promoted before we query no_response
    from app.services.campaign_tracking import promote_no_response_statuses

    promote_no_response_statuses(db, campaign_id)
    db.flush()

    days = settings.followup_no_response_days
    cutoff = datetime.utcnow() - timedelta(days=days)
    created: list[FollowUpProposal] = []

    # No-response follow-ups
    cands = (
        db.query(CampaignCandidate)
        .filter(
            CampaignCandidate.campaign_id == campaign_id,
            CampaignCandidate.decision == "include",
            CampaignCandidate.tracking_status == "no_response",
        )
        .all()
    )
    for cand in cands:
        existing = (
            db.query(FollowUpProposal)
            .filter(
                FollowUpProposal.campaign_id == campaign_id,
                FollowUpProposal.candidate_id == cand.id,
                FollowUpProposal.kind == "no_response",
                FollowUpProposal.status.in_(("proposed", "approved")),
            )
            .first()
        )
        if existing:
            continue
        sent = (
            db.query(SendLog)
            .filter(
                SendLog.campaign_id == campaign_id,
                SendLog.candidate_id == cand.id,
                SendLog.action == "sent",
            )
            .order_by(SendLog.sent_at.desc())
            .first()
        )
        if sent and sent.sent_at and sent.sent_at > cutoff:
            continue
        first = (cand.full_name or "there").split()[0]
        body = (
            f"Hi {first},\n\n"
            f"Just floating this back up in case it got buried — "
            f"happy to make it easy whenever timing works.\n\n"
            f"Best regards"
        )
        row = FollowUpProposal(
            campaign_id=campaign_id,
            candidate_id=cand.id,
            kind="no_response",
            subject=f"Following up — {cand.full_name or first}",
            body=body,
            status="proposed",
            based_on_status="no_response",
        )
        db.add(row)
        created.append(row)

    # Commitment nudges
    commits = (
        db.query(CampaignCommitment)
        .filter(
            CampaignCommitment.campaign_id == campaign_id,
            CampaignCommitment.status == "open",
            CampaignCommitment.owner == "theirs",
        )
        .all()
    )
    for commit in commits:
        existing = (
            db.query(FollowUpProposal)
            .filter(
                FollowUpProposal.campaign_id == campaign_id,
                FollowUpProposal.candidate_id == commit.candidate_id,
                FollowUpProposal.kind == "commitment_nudge",
                FollowUpProposal.status.in_(("proposed", "approved")),
            )
            .first()
        )
        if existing:
            continue
        cand = db.get(CampaignCandidate, commit.candidate_id)
        if not cand:
            continue
        first = (cand.full_name or "there").split()[0]
        body = (
            f"Hi {first},\n\n"
            f"Circling back on: {commit.text[:160]}\n\n"
            f"No rush — just wanted to stay in sync.\n\nBest regards"
        )
        row = FollowUpProposal(
            campaign_id=campaign_id,
            candidate_id=cand.id,
            kind="commitment_nudge",
            subject=f"Quick nudge — {cand.full_name or first}",
            body=body,
            status="proposed",
            based_on_status="commitment",
        )
        db.add(row)
        created.append(row)

    # Intro tasks (checklist — not email until converted)
    intro_cands = (
        db.query(CampaignCandidate)
        .filter(
            CampaignCandidate.campaign_id == campaign_id,
            CampaignCandidate.tracking_status == "intro_offered",
        )
        .all()
    )
    for cand in intro_cands:
        existing = (
            db.query(FollowUpProposal)
            .filter(
                FollowUpProposal.campaign_id == campaign_id,
                FollowUpProposal.candidate_id == cand.id,
                FollowUpProposal.kind == "intro_task",
                FollowUpProposal.status.in_(("proposed", "approved")),
            )
            .first()
        )
        if existing:
            continue
        row = FollowUpProposal(
            campaign_id=campaign_id,
            candidate_id=cand.id,
            kind="intro_task",
            subject=f"Follow through on intro — {cand.full_name}",
            body="Task: confirm intro details and send a thank-you / next-step note when ready.",
            status="proposed",
            based_on_status="intro_offered",
        )
        db.add(row)
        created.append(row)

    audit(
        db,
        campaign_id,
        "followups_proposed",
        f"Proposed {len(created)} follow-up(s) — nothing sent",
        {"count": len(created)},
    )
    db.commit()
    for row in created:
        db.refresh(row)
    return created


def list_followups(db: Session, campaign_id: str) -> list[dict[str, Any]]:
    rows = (
        db.query(FollowUpProposal)
        .filter(FollowUpProposal.campaign_id == campaign_id)
        .order_by(FollowUpProposal.created_at.desc())
        .all()
    )
    return [_proposal_to_dict(p) for p in rows]


def set_followup_status(
    db: Session, campaign_id: str, followup_id: str, status: str
) -> FollowUpProposal:
    if status not in ("approved", "rejected", "cancelled"):
        raise SendGateError("Invalid status", 400)
    row = db.get(FollowUpProposal, followup_id)
    if not row or row.campaign_id != campaign_id:
        raise SendGateError("Follow-up not found", 404)
    if row.status == "sent":
        raise SendGateError("Already sent", 409)
    row.status = status
    row.updated_at = datetime.utcnow()
    audit(db, campaign_id, "followup_status", f"Follow-up {followup_id} → {status}", {})
    db.commit()
    db.refresh(row)
    return row


async def authorize_followup_send(
    db: Session,
    campaign_id: str,
    followup_id: str,
    *,
    confirm: bool,
    recipient_email: str | None = None,
) -> dict[str, Any]:
    """Gate 9 — distinct from draft approval and from propose."""
    settings = get_settings()
    if not settings.feature_compass_followups:
        raise SendGateError("FEATURE_COMPASS_FOLLOWUPS is disabled", 403)
    if not settings.feature_compass_send:
        raise SendGateError(
            "Proposals-only mode: set FEATURE_COMPASS_SEND=true to send follow-ups",
            403,
        )
    if not confirm:
        raise SendGateError("Gate 9 requires confirm=true", 400)

    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise SendGateError("Campaign not found", 404)
    account_id = require_sending_account(campaign)

    row = db.get(FollowUpProposal, followup_id)
    if not row or row.campaign_id != campaign_id:
        raise SendGateError("Follow-up not found", 404)
    if row.kind == "intro_task":
        raise SendGateError("Intro tasks are not emails — convert to a message first", 400)
    if row.status not in ("proposed", "approved"):
        raise SendGateError("Follow-up not sendable in current status", 409)

    cand = db.get(CampaignCandidate, row.candidate_id)
    if not cand or not cand.email:
        raise SendGateError("Candidate email missing", 400)
    expected = cand.email.lower().strip()
    if recipient_email and recipient_email.lower().strip() != expected:
        raise SendGateError("Recipient restatement mismatch", 400)

    from app.services.campaign_send import _send_provider_mail

    try:
        meta = await _send_provider_mail(
            db,
            account_id=account_id,
            to_email=cand.email,
            to_name=cand.full_name,
            subject=row.subject or "",
            body=row.body or "",
        )
    except Exception as exc:
        log = SendLog(
            campaign_id=campaign_id,
            draft_id=None,
            candidate_id=cand.id,
            account_id=account_id,
            recipient=cand.email,
            subject=row.subject,
            body_hash=body_hash(row.body),
            action="failed",
            error=str(exc),
            authorized_at=datetime.utcnow(),
            authorized_by="user",
        )
        db.add(log)
        db.commit()
        raise SendGateError(f"Send failed: {exc}", 502) from exc

    log = SendLog(
        campaign_id=campaign_id,
        draft_id=None,
        candidate_id=cand.id,
        account_id=account_id,
        recipient=cand.email,
        subject=row.subject,
        body_hash=body_hash(row.body),
        action="sent",
        sent_at=datetime.utcnow(),
        authorized_at=datetime.utcnow(),
        authorized_by="user",
        conversation_id=(meta or {}).get("conversation_id"),
        internet_message_id=(meta or {}).get("internet_message_id"),
        provider_message_id=(meta or {}).get("id"),
    )
    db.add(log)
    row.status = "sent"
    row.gate9_authorized_at = datetime.utcnow()
    cand.tracking_status = "sent"
    audit(db, campaign_id, "gate9_send", f"Gate 9 sent follow-up {followup_id}", {})
    db.commit()
    return {"status": "sent", "followup_id": followup_id, "email": cand.email}
