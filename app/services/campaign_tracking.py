"""P10 — campaign reply matching, status dashboard, commitment extraction."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.campaign import (
    Campaign,
    CampaignCandidate,
    CampaignCommitment,
    CampaignReply,
    SendLog,
)
from app.models.message import EmailMessage
from app.services.campaign_service import audit

logger = logging.getLogger(__name__)

INTRO_RE = re.compile(r"\b(introduc|connect you with|put you in touch)\b", re.I)
MEETING_RE = re.compile(r"\b(let'?s (meet|talk|schedule)|book a call|calendar invite)\b", re.I)
DECLINE_RE = re.compile(r"\b(not interested|pass for now|no thank|can'?t help)\b", re.I)
COMMIT_RE = re.compile(
    r"\b(i('ll| will) (send|share|forward)|promise[sd]?|memo|deck|follow up on)\b",
    re.I,
)


def _normalize_subject(subject: str | None) -> str:
    s = (subject or "").lower().strip()
    s = re.sub(r"^(re|fw|fwd):\s*", "", s)
    while True:
        nxt = re.sub(r"^(re|fw|fwd):\s*", "", s)
        if nxt == s:
            break
        s = nxt
    return s.strip()


def _excerpt(msg: EmailMessage) -> str:
    text = (msg.body_preview or msg.subject or "").strip()
    return text[:280]


def match_inbound_to_send_log(
    db: Session,
    msg: EmailMessage,
    *,
    campaign_id: str | None = None,
) -> tuple[SendLog | None, str | None]:
    """Return (send_log, matched_by) with precision-first matching."""
    if (msg.direction or "").lower() != "inbound":
        return None, None

    q = db.query(SendLog).filter(SendLog.action == "sent")
    if campaign_id:
        q = q.filter(SendLog.campaign_id == campaign_id)
    if msg.source_account:
        q = q.filter(SendLog.account_id == msg.source_account)

    logs = q.all()
    if not logs:
        return None, None

    # 1) conversation_id exact
    if msg.conversation_id:
        for log in logs:
            if log.conversation_id and log.conversation_id == msg.conversation_id:
                return log, "conversation_id"

    # 2) internet_message_id / in-reply-to style (stored id appears in preview rarely;
    #    match if message internet id equals outbound id is uncommon for replies —
    #    also try subject+recipient)
    sender = (msg.sender_email or "").lower().strip()
    subj = _normalize_subject(msg.subject)
    window_start = msg.sent_datetime - timedelta(days=60) if msg.sent_datetime else None

    candidates: list[SendLog] = []
    for log in logs:
        if not log.recipient or log.recipient.lower().strip() != sender:
            continue
        if window_start and log.sent_at and log.sent_at < window_start:
            continue
        if log.sent_at and msg.sent_datetime and log.sent_at > msg.sent_datetime:
            continue
        log_subj = _normalize_subject(log.subject)
        if subj and log_subj and (subj == log_subj or subj in log_subj or log_subj in subj):
            candidates.append(log)

    if len(candidates) == 1:
        return candidates[0], "subject_recipient"
    # Ambiguous → no match (precision over recall)
    return None, None


def _infer_status_from_reply(excerpt: str) -> str:
    if DECLINE_RE.search(excerpt or ""):
        return "declined"
    if INTRO_RE.search(excerpt or ""):
        return "intro_offered"
    if MEETING_RE.search(excerpt or ""):
        return "meeting_booked"
    return "replied"


def _extract_commitments_heuristic(excerpt: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    text = excerpt or ""
    if COMMIT_RE.search(text):
        out.append(
            {
                "owner": "theirs",
                "text": text[:200],
                "due_hint": "soon",
            }
        )
    if MEETING_RE.search(text):
        out.append(
            {
                "owner": "ours",
                "text": "Follow through on proposed meeting",
                "due_hint": "this week",
            }
        )
    return out


def refresh_campaign_tracking(db: Session, campaign_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.feature_compass_tracking:
        raise ValueError("FEATURE_COMPASS_TRACKING is disabled")

    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("Campaign not found")

    account_id = campaign.sending_account_id
    sent_logs = (
        db.query(SendLog)
        .filter(SendLog.campaign_id == campaign_id, SendLog.action == "sent")
        .all()
    )

    # Mark no_response for aged sent without reply
    days = settings.followup_no_response_days
    cutoff = datetime.utcnow() - timedelta(days=days)
    existing_reply_msg_ids = {
        r.message_id
        for r in db.query(CampaignReply).filter(CampaignReply.campaign_id == campaign_id).all()
        if r.message_id
    }

    inbound_q = db.query(EmailMessage).filter(EmailMessage.direction == "inbound")
    if account_id:
        inbound_q = inbound_q.filter(EmailMessage.source_account == account_id)
    inbound = inbound_q.order_by(EmailMessage.sent_datetime.desc()).limit(500).all()

    matched = 0
    for msg in inbound:
        if msg.id in existing_reply_msg_ids:
            continue
        log, how = match_inbound_to_send_log(db, msg, campaign_id=campaign_id)
        if not log or not how:
            continue
        excerpt = _excerpt(msg)
        reply = CampaignReply(
            campaign_id=campaign_id,
            candidate_id=log.candidate_id or "",
            send_log_id=log.id,
            message_id=msg.id,
            matched_by=how,
            excerpt=excerpt,
            matched_at=datetime.utcnow(),
        )
        if not reply.candidate_id:
            continue
        db.add(reply)
        matched += 1
        cand = db.get(CampaignCandidate, reply.candidate_id)
        if cand:
            cand.tracking_status = _infer_status_from_reply(excerpt)
        for c in _extract_commitments_heuristic(excerpt):
            db.add(
                CampaignCommitment(
                    campaign_id=campaign_id,
                    candidate_id=reply.candidate_id,
                    reply_id=None,
                    owner=c["owner"],
                    text=c["text"],
                    due_hint=c.get("due_hint"),
                    status="open",
                )
            )

    replied_cand_ids = {
        r.candidate_id
        for r in db.query(CampaignReply).filter(CampaignReply.campaign_id == campaign_id).all()
    }
    for log in sent_logs:
        if not log.candidate_id or log.candidate_id in replied_cand_ids:
            continue
        cand = db.get(CampaignCandidate, log.candidate_id)
        if not cand:
            continue
        if cand.tracking_status in (
            "replied",
            "intro_offered",
            "meeting_booked",
            "declined",
        ):
            continue
        if log.sent_at and log.sent_at <= cutoff:
            cand.tracking_status = "no_response"
        elif not cand.tracking_status or cand.tracking_status in ("saved", "scheduled", "drafted"):
            cand.tracking_status = "sent"

    campaign.status = "tracking"
    audit(db, campaign_id, "tracking_refreshed", f"Matched {matched} new reply(ies)", {})
    db.commit()
    return tracking_dashboard(db, campaign_id)


def tracking_dashboard(db: Session, campaign_id: str) -> dict[str, Any]:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("Campaign not found")

    cands = (
        db.query(CampaignCandidate)
        .filter(
            CampaignCandidate.campaign_id == campaign_id,
            CampaignCandidate.decision == "include",
        )
        .order_by(CampaignCandidate.rank.asc())
        .all()
    )
    replies = (
        db.query(CampaignReply)
        .filter(CampaignReply.campaign_id == campaign_id)
        .order_by(CampaignReply.matched_at.desc())
        .all()
    )
    commitments = (
        db.query(CampaignCommitment)
        .filter(CampaignCommitment.campaign_id == campaign_id)
        .order_by(CampaignCommitment.created_at.desc())
        .all()
    )
    sent_count = (
        db.query(SendLog)
        .filter(SendLog.campaign_id == campaign_id, SendLog.action == "sent")
        .count()
    )

    counts: dict[str, int] = {
        "sent": sent_count,
        "replied": 0,
        "intro_offered": 0,
        "meeting_booked": 0,
        "declined": 0,
        "no_response": 0,
        "drafted": 0,
        "saved": 0,
        "scheduled": 0,
    }
    contacts = []
    for c in cands:
        st = c.tracking_status or "drafted"
        if st in counts:
            counts[st] += 1
        contacts.append(
            {
                "candidate_id": c.id,
                "name": c.full_name,
                "email": c.email,
                "tracking_status": st,
                "company": c.company,
            }
        )

    return {
        "campaign_id": campaign_id,
        "title": campaign.title,
        "status": campaign.status,
        "sending_account_id": campaign.sending_account_id,
        "counts": counts,
        "contacts": contacts,
        "replies": [
            {
                "id": r.id,
                "candidate_id": r.candidate_id,
                "excerpt": r.excerpt,
                "matched_by": r.matched_by,
                "matched_at": r.matched_at.isoformat() if r.matched_at else None,
            }
            for r in replies
        ],
        "commitments": [
            {
                "id": c.id,
                "candidate_id": c.candidate_id,
                "owner": c.owner,
                "text": c.text,
                "due_hint": c.due_hint,
                "status": c.status,
            }
            for c in commitments
        ],
        "suggestions": _suggestions(counts, commitments),
    }


def _suggestions(counts: dict[str, int], commitments: list[CampaignCommitment]) -> list[str]:
    out: list[str] = []
    if counts.get("no_response"):
        out.append(
            f"{counts['no_response']} no-response after "
            f"{get_settings().followup_no_response_days} days → Review follow-up drafts"
        )
    if counts.get("intro_offered"):
        out.append(f"{counts['intro_offered']} intro offer(s) → Create follow-through tasks")
    open_c = [c for c in commitments if c.status == "open"]
    if open_c:
        out.append(f"{open_c[0].text[:80]} — nudge?")
    return out


def list_campaigns_summary(db: Session) -> list[dict[str, Any]]:
    rows = db.query(Campaign).order_by(Campaign.updated_at.desc()).limit(50).all()
    out = []
    for c in rows:
        sent = (
            db.query(SendLog)
            .filter(SendLog.campaign_id == c.id, SendLog.action == "sent")
            .count()
        )
        replied = (
            db.query(CampaignReply).filter(CampaignReply.campaign_id == c.id).count()
        )
        out.append(
            {
                "id": c.id,
                "title": c.title or (c.objective_raw or "")[:60],
                "status": c.status,
                "objective_raw": c.objective_raw,
                "sent": sent,
                "replied": replied,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
        )
    return out
