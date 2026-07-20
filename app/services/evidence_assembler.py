"""P4 — assemble citable evidence; drop uncited claims."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.contact import Contact, ContactEmailLink
from app.models.message import EmailMessage


def gather_message_evidence(
    db: Session,
    contact: Contact,
    *,
    account_ids: list[str],
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Pull real messages linked to the contact within scoped accounts."""
    q = (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact.id)
    )
    if account_ids:
        q = q.filter(EmailMessage.source_account.in_(account_ids))
    rows = (
        q.order_by(EmailMessage.sent_datetime.desc())
        .limit(limit)
        .all()
    )
    items: list[dict[str, Any]] = []
    for msg in rows:
        if not msg.id:
            continue
        preview = (msg.body_preview or "").strip()
        summary = preview[:180] if preview else (msg.subject or "Email exchange")
        items.append(
            {
                "kind": "email",
                "occurred_at": msg.sent_datetime,
                "source_account": msg.source_account or "edge",
                "direction": msg.direction or "outbound",
                "subject": msg.subject,
                "summary": summary,
                "message_id": msg.id,
                "outlook_weblink": msg.outlook_weblink,
                "citation_ok": True,
            }
        )
    return items


def validate_evidence_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Server-side citation gate: require message_id (or equivalent) for email claims."""
    ok: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("kind") or "email"
        if kind == "email" and not item.get("message_id"):
            continue  # drop uncited
        if not item.get("summary") and not item.get("subject"):
            continue
        item = dict(item)
        item["citation_ok"] = True
        ok.append(item)
    return ok


def build_why_text(contact: Contact, evidence: list[dict[str, Any]]) -> str:
    """Prose explanation grounded only in cited evidence + contact aggregates."""
    parts: list[str] = []
    n = len(evidence)
    emails = contact.email_count or 0
    threads = contact.thread_count or 0
    if emails or threads:
        parts.append(
            f"You exchanged about {emails} emails across {threads} threads"
        )
    if contact.first_contacted_at and contact.last_contacted_at:
        parts.append(
            f"from {contact.first_contacted_at.strftime('%b %Y')} "
            f"to {contact.last_contacted_at.strftime('%b %Y')}"
        )
    if evidence:
        latest = evidence[0]
        when = latest.get("occurred_at")
        when_s = when.strftime("%b %Y") if isinstance(when, datetime) else "recently"
        subj = latest.get("subject") or "a recent thread"
        parts.append(f"Most recent cited exchange ({when_s}): {subj}")
        if n > 1:
            parts.append(f"{n} citable messages support this recommendation")
    elif contact.outreach_score_explanation:
        # Only use stored explanation if we still have at least aggregate facts
        parts.append("Ranking also reflects your stored outreach analysis")
    if not parts:
        return "Limited mailbox evidence — review carefully before including."
    # Join into one readable paragraph
    text = parts[0]
    for p in parts[1:]:
        if p[0].islower() or p.startswith("from ") or p.startswith("to "):
            text += " " + p
        else:
            text += ". " + p
    if not text.endswith("."):
        text += "."
    return text


def drop_uncited_claims_from_why(why: str, evidence: list[dict[str, Any]]) -> str:
    """If no evidence, strip invented specifics — keep honest insufficiency."""
    if evidence:
        return why
    return "Limited mailbox evidence — review carefully before including."
