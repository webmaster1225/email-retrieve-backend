"""P8–P9 — sending account, save-to-mailbox, schedule, Gate 8 send."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models.campaign import Campaign, CampaignCandidate, CampaignDraft, SendLog
from app.services.campaign_drafting import body_hash
from app.services.campaign_service import audit

logger = logging.getLogger(__name__)


class SendGateError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def require_sending_account(campaign: Campaign) -> str:
    if not campaign.sending_account_id or not campaign.sending_account_confirmed_at:
        raise SendGateError("Sending account not confirmed (Gate 5)", 409)
    return campaign.sending_account_id


def confirm_sending_account(
    db: Session,
    campaign: Campaign,
    *,
    account_id: str,
    careers_justification: str | None = None,
) -> Campaign:
    allowed = set(campaign.account_ids or [])
    if account_id not in allowed and account_id not in ("edge", "galaxy", "careers", "northwyn"):
        raise SendGateError("Unknown sending account", 400)
    if account_id == "careers" and not (careers_justification or "").strip():
        raise SendGateError("Careers mailbox requires an explicit justification", 400)
    campaign.sending_account_id = account_id
    campaign.sending_account_confirmed_at = datetime.utcnow()
    campaign.careers_justification = careers_justification
    campaign.status = "ready_to_save"
    audit(
        db,
        campaign.id,
        "sending_account_confirmed",
        f"Gate 5: sending account set to {account_id}",
        {"account_id": account_id},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


async def save_drafts_to_mailbox(db: Session, campaign_id: str) -> list[dict[str, Any]]:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise SendGateError("Campaign not found", 404)
    account_id = require_sending_account(campaign)

    drafts = (
        db.query(CampaignDraft)
        .options(joinedload(CampaignDraft.candidate))
        .filter(
            CampaignDraft.campaign_id == campaign_id,
            CampaignDraft.status == "approved",
            CampaignDraft.variant == "email",
        )
        .all()
    )
    if not drafts:
        raise SendGateError("No approved email drafts to save", 400)

    results: list[dict[str, Any]] = []
    for draft in drafts:
        acct = draft.sending_account_override or account_id
        cand = draft.candidate
        try:
            meta = await _create_provider_draft(
                db,
                account_id=acct,
                to_email=cand.email if cand else None,
                to_name=cand.full_name if cand else None,
                subject=draft.subject or "",
                body=draft.body or "",
            )
            draft.mailbox_draft_id = meta.get("id")
            draft.mailbox_draft_web_link = meta.get("web_link")
            draft.lifecycle = "saved"
            if cand:
                cand.tracking_status = "saved"
            results.append({"draft_id": draft.id, "status": "saved", **meta})
        except Exception as exc:
            logger.exception("Save draft failed")
            results.append({"draft_id": draft.id, "status": "failed", "error": str(exc)})
    if any(r.get("status") == "saved" for r in results):
        campaign.status = "tracking"
    audit(db, campaign_id, "drafts_saved", f"Saved {len(results)} draft(s) to {account_id}", {"results": results})
    db.commit()
    return results


async def _create_provider_draft(
    db: Session,
    *,
    account_id: str,
    to_email: str | None,
    to_name: str | None,
    subject: str,
    body: str,
) -> dict[str, Any]:
    if account_id == "northwyn":
        from app.services.gmail_client import GmailClient

        client = GmailClient(db, account_id="northwyn")
        return await client.create_draft(
            to_email=to_email or "",
            to_name=to_name,
            subject=subject,
            body=body,
        )
    from app.services.graph_client import GraphClient

    client = GraphClient(db, account_id=account_id)
    return await client.create_draft(
        to_email=to_email or "",
        to_name=to_name,
        subject=subject,
        body=body,
    )


def send_preview(db: Session, campaign_id: str) -> dict[str, Any]:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise SendGateError("Campaign not found", 404)
    account_id = require_sending_account(campaign)
    drafts = (
        db.query(CampaignDraft)
        .options(joinedload(CampaignDraft.candidate))
        .filter(
            CampaignDraft.campaign_id == campaign_id,
            CampaignDraft.status == "approved",
            CampaignDraft.variant == "email",
        )
        .all()
    )
    recipients = []
    for d in drafts:
        email = d.candidate.email if d.candidate else None
        if email:
            recipients.append(
                {
                    "draft_id": d.id,
                    "email": email,
                    "name": d.candidate.full_name if d.candidate else None,
                    "subject": d.subject,
                }
            )
    return {
        "account_id": account_id,
        "recipients": recipients,
        "recipient_emails": [r["email"] for r in recipients],
        "count": len(recipients),
    }


def schedule_sends(
    db: Session,
    campaign_id: str,
    *,
    scheduled_for: datetime,
    authorized_by: str = "user",
) -> list[SendLog]:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise SendGateError("Campaign not found", 404)
    account_id = require_sending_account(campaign)
    preview = send_preview(db, campaign_id)
    logs: list[SendLog] = []
    for r in preview["recipients"]:
        draft = db.get(CampaignDraft, r["draft_id"])
        log = SendLog(
            campaign_id=campaign_id,
            draft_id=r["draft_id"],
            candidate_id=draft.candidate_id if draft else None,
            account_id=account_id,
            recipient=r["email"],
            subject=r.get("subject"),
            body_hash=body_hash(draft.body if draft else ""),
            action="scheduled",
            scheduled_for=scheduled_for,
            authorized_at=datetime.utcnow(),
            authorized_by=authorized_by,
        )
        db.add(log)
        if draft:
            draft.lifecycle = "scheduled"
            if draft.candidate_id:
                cand = db.get(CampaignCandidate, draft.candidate_id)
                if cand:
                    cand.tracking_status = "scheduled"
        logs.append(log)
    campaign.status = "scheduled"
    audit(db, campaign_id, "sends_scheduled", f"Scheduled {len(logs)} message(s)", {})
    db.commit()
    return logs


def cancel_schedule(db: Session, campaign_id: str, send_log_id: str) -> SendLog:
    log = db.get(SendLog, send_log_id)
    if not log or log.campaign_id != campaign_id:
        raise SendGateError("Schedule entry not found", 404)
    if log.action != "scheduled":
        raise SendGateError("Only scheduled entries can be cancelled", 400)
    log.action = "cancelled"
    if log.draft_id:
        draft = db.get(CampaignDraft, log.draft_id)
        if draft and draft.lifecycle == "scheduled":
            draft.lifecycle = "saved"
    audit(db, campaign_id, "schedule_cancelled", f"Cancelled {send_log_id}", {})
    db.commit()
    db.refresh(log)
    return log


def _apply_send_meta(log: SendLog, meta: dict[str, Any] | None) -> None:
    if not meta:
        return
    log.provider_message_id = meta.get("id") or log.provider_message_id
    log.conversation_id = meta.get("conversation_id") or log.conversation_id
    log.internet_message_id = meta.get("internet_message_id") or log.internet_message_id


async def authorize_send(
    db: Session,
    campaign_id: str,
    *,
    confirm: bool,
    recipient_emails: list[str],
    authorized_by: str = "user",
) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.feature_compass_send:
        raise SendGateError(
            "Sending disabled. Set FEATURE_COMPASS_SEND=true to enable Gate 8.",
            403,
        )
    if not confirm:
        raise SendGateError("Gate 8 requires confirm=true", 400)

    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise SendGateError("Campaign not found", 404)
    account_id = require_sending_account(campaign)
    preview = send_preview(db, campaign_id)
    expected = sorted(preview["recipient_emails"])
    provided = sorted(recipient_emails or [])
    if expected != provided:
        raise SendGateError(
            "Recipient restatement mismatch — re-check the send preview list",
            400,
        )

    results: list[dict[str, Any]] = []
    for r in preview["recipients"]:
        draft = db.get(CampaignDraft, r["draft_id"])
        if not draft:
            continue
        try:
            meta = await _send_provider_mail(
                db,
                account_id=draft.sending_account_override or account_id,
                to_email=r["email"],
                to_name=r.get("name"),
                subject=draft.subject or "",
                body=draft.body or "",
            )
            log = SendLog(
                campaign_id=campaign_id,
                draft_id=draft.id,
                candidate_id=draft.candidate_id,
                account_id=account_id,
                recipient=r["email"],
                subject=draft.subject,
                body_hash=body_hash(draft.body),
                action="sent",
                sent_at=datetime.utcnow(),
                authorized_at=datetime.utcnow(),
                authorized_by=authorized_by,
            )
            _apply_send_meta(log, meta)
            db.add(log)
            draft.lifecycle = "sent"
            if draft.candidate_id:
                cand = db.get(CampaignCandidate, draft.candidate_id)
                if cand:
                    cand.tracking_status = "sent"
            results.append({"draft_id": draft.id, "email": r["email"], "status": "sent"})
        except Exception as exc:
            log = SendLog(
                campaign_id=campaign_id,
                draft_id=draft.id,
                candidate_id=draft.candidate_id,
                account_id=account_id,
                recipient=r["email"],
                subject=draft.subject,
                body_hash=body_hash(draft.body),
                action="failed",
                error=str(exc),
                authorized_at=datetime.utcnow(),
                authorized_by=authorized_by,
            )
            db.add(log)
            results.append(
                {"draft_id": draft.id, "email": r["email"], "status": "failed", "error": str(exc)}
            )
    campaign.status = "tracking"
    audit(db, campaign_id, "gate8_send", "Gate 8 send authorized", {"results": results})
    db.commit()
    return results


async def _send_provider_mail(
    db: Session,
    *,
    account_id: str,
    to_email: str,
    to_name: str | None,
    subject: str,
    body: str,
) -> dict[str, Any]:
    if account_id == "northwyn":
        from app.services.gmail_client import GmailClient

        client = GmailClient(db, account_id="northwyn")
        return await client.send_mail(
            to_email=to_email, to_name=to_name, subject=subject, body=body
        )
    from app.services.graph_client import GraphClient

    client = GraphClient(db, account_id=account_id)
    return await client.send_mail(
        to_email=to_email, to_name=to_name, subject=subject, body=body
    )


async def process_due_scheduled(db: Session) -> int:
    """Send any scheduled messages that are due (Gate 7 worker)."""
    if not get_settings().feature_compass_send:
        return 0
    now = datetime.utcnow()
    due = (
        db.query(SendLog)
        .filter(SendLog.action == "scheduled", SendLog.scheduled_for <= now)
        .all()
    )
    count = 0
    for log in due:
        draft = db.get(CampaignDraft, log.draft_id) if log.draft_id else None
        if not draft:
            log.action = "failed"
            log.error = "Draft missing"
            continue
        try:
            meta = await _send_provider_mail(
                db,
                account_id=log.account_id or "edge",
                to_email=log.recipient or "",
                to_name=None,
                subject=draft.subject or "",
                body=draft.body or "",
            )
            log.action = "sent"
            log.sent_at = datetime.utcnow()
            _apply_send_meta(log, meta)
            draft.lifecycle = "sent"
            if draft.candidate_id:
                cand = db.get(CampaignCandidate, draft.candidate_id)
                if cand:
                    cand.tracking_status = "sent"
            campaign = db.get(Campaign, log.campaign_id)
            if campaign:
                campaign.status = "tracking"
            count += 1
        except Exception as exc:
            log.action = "failed"
            log.error = str(exc)
    db.commit()
    return count
