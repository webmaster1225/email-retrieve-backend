from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session, joinedload

from app.models.contact import Contact, ContactEmailLink
from app.models.message import EmailMessage
from app.models.sync import AuthToken
from app.services.graph_client import GraphAuthError, GraphClient
from app.services.text_utils import normalize_email

MAX_EXCHANGE_MESSAGES = 50
MAX_AI_SAMPLES = 20
_EPOCH_UTC = datetime.min.replace(tzinfo=timezone.utc)


def _coerce_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sender_email(item: dict) -> str | None:
    for key in ("from", "sender"):
        addr = (item.get(key) or {}).get("emailAddress", {}).get("address")
        if addr:
            return normalize_email(addr)
    return None


def _recipient_emails(item: dict) -> set[str]:
    emails: set[str] = set()
    for field in ("toRecipients", "ccRecipients", "bccRecipients"):
        for recipient in item.get(field) or []:
            addr = normalize_email((recipient.get("emailAddress") or {}).get("address"))
            if addr:
                emails.add(addr)
    return emails


def classify_message_direction(item: dict, contact_email: str, user_email: str | None) -> str:
    contact = normalize_email(contact_email)
    user = normalize_email(user_email)
    sender = _sender_email(item)
    if sender == contact:
        return "inbound"
    if user and sender == user:
        return "outbound"
    recipients = _recipient_emails(item)
    if contact in recipients:
        return "outbound"
    return "unknown"


def _parse_graph_datetime(item: dict) -> datetime | None:
    raw = item.get("sentDateTime") or item.get("receivedDateTime")
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _normalize_graph_message(item: dict, contact_email: str, user_email: str | None) -> dict:
    sent_dt = _parse_graph_datetime(item)
    return {
        "graph_message_id": item.get("id"),
        "direction": classify_message_direction(item, contact_email, user_email),
        "subject": item.get("subject"),
        "body_preview": item.get("bodyPreview"),
        "conversation_id": item.get("conversationId"),
        "sent_datetime": _coerce_utc(sent_dt),
        "has_attachments": bool(item.get("hasAttachments")),
        "outlook_weblink": item.get("webLink"),
    }


def _normalize_local_message(message: EmailMessage) -> dict:
    return {
        "graph_message_id": message.graph_message_id,
        "direction": message.direction or "outbound",
        "subject": message.subject,
        "body_preview": message.body_preview,
        "conversation_id": message.conversation_id,
        "sent_datetime": _coerce_utc(message.sent_datetime),
        "has_attachments": message.has_attachments,
        "outlook_weblink": message.outlook_weblink,
    }


def _merge_messages(*groups: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for group in groups:
        for message in group:
            key = message.get("graph_message_id") or (
                f"{message.get('sent_datetime')}|{message.get('subject')}|{message.get('direction')}"
            )
            if key not in by_id:
                by_id[key] = message
    merged = list(by_id.values())
    merged.sort(
        key=lambda m: _coerce_utc(m.get("sent_datetime")) or _EPOCH_UTC,
        reverse=True,
    )
    return merged[:MAX_EXCHANGE_MESSAGES]


def compute_exchange_stats(messages: list[dict], *, data_source: str) -> dict:
    outbound = [m for m in messages if m.get("direction") == "outbound"]
    inbound = [m for m in messages if m.get("direction") == "inbound"]
    threads: dict[str, set[str]] = defaultdict(set)
    for message in messages:
        conversation_id = message.get("conversation_id")
        if conversation_id:
            threads[conversation_id].add(message.get("direction") or "unknown")

    two_way_threads = sum(
        1 for directions in threads.values() if "inbound" in directions and "outbound" in directions
    )
    dates = [_coerce_utc(m["sent_datetime"]) for m in messages if m.get("sent_datetime")]
    first_exchange_at = min(dates).isoformat() if dates else None
    last_exchange_at = max(dates).isoformat() if dates else None

    return {
        "outbound_count": len(outbound),
        "inbound_count": len(inbound),
        "total_count": len(messages),
        "thread_count": len(threads) or (1 if messages else 0),
        "two_way_threads": two_way_threads,
        "has_two_way": len(outbound) > 0 and len(inbound) > 0,
        "first_exchange_at": first_exchange_at,
        "last_exchange_at": last_exchange_at,
        "data_source": data_source,
    }


def build_exchange_prompt_context(
    *,
    contact_email: str,
    full_name: str | None,
    company_name: str | None,
    stats: dict,
    messages: list[dict],
) -> str:
    lines = [
        f"Contact: {full_name or 'Unknown'} <{contact_email}>",
        f"Company: {company_name or 'Unknown'}",
        "",
        "Email exchange statistics:",
        f"- Outbound (we sent): {stats['outbound_count']}",
        f"- Inbound (they sent): {stats['inbound_count']}",
        f"- Total messages analyzed: {stats['total_count']}",
        f"- Conversation threads: {stats['thread_count']}",
        f"- Threads with back-and-forth: {stats['two_way_threads']}",
        f"- Two-way relationship: {'yes' if stats['has_two_way'] else 'no'}",
        f"- First exchange: {stats.get('first_exchange_at') or 'unknown'}",
        f"- Last exchange: {stats.get('last_exchange_at') or 'unknown'}",
        "",
        "Message samples (newest first):",
    ]
    for message in messages[:MAX_AI_SAMPLES]:
        sent = message.get("sent_datetime")
        sent_label = sent.strftime("%Y-%m-%d") if sent else "unknown date"
        direction = message.get("direction") or "unknown"
        lines.append(
            f"- [{sent_label}] {direction.upper()} | Subject: {message.get('subject') or '(no subject)'}\n"
            f"  Preview: {message.get('body_preview') or '(empty)'}"
        )
    if not messages:
        lines.append("- No message content available")
    return "\n".join(lines)


def gather_local_exchange(db: Session, contact_id: str) -> tuple[list[dict], str]:
    messages = (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id)
        .order_by(EmailMessage.sent_datetime.desc())
        .limit(MAX_EXCHANGE_MESSAGES)
        .all()
    )
    normalized = [_normalize_local_message(message) for message in messages]
    return normalized, "local"


async def gather_exchange_data(
    db: Session,
    contact_email: str,
    *,
    full_name: str | None = None,
    company_name: str | None = None,
    last_subject: str | None = None,
    last_preview: str | None = None,
) -> tuple[dict, list[dict]]:
    email = normalize_email(contact_email)
    if not email:
        raise ValueError("Invalid email address")

    local_messages: list[dict] = []
    local = (
        db.query(Contact)
        .options(joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.primary_email == email)
        .one_or_none()
    )
    if local:
        local_messages, _ = gather_local_exchange(db, local.id)

    graph_messages: list[dict] = []
    data_source = "local" if local_messages else "outlook_list"
    token_row = db.query(AuthToken).order_by(AuthToken.updated_at.desc()).first()
    user_email = normalize_email(token_row.user_email) if token_row and token_row.user_email else None

    graph = GraphClient(db)
    try:
        graph.ensure_access_token()
        raw_items = await graph.search_messages_with_participant(email, top=MAX_EXCHANGE_MESSAGES)
        graph_messages = [_normalize_graph_message(item, email, user_email) for item in raw_items]
        if graph_messages:
            data_source = "graph" if not local_messages else "graph+local"
    except (GraphAuthError, httpx.HTTPError):
        pass

    merged = _merge_messages(local_messages, graph_messages)
    if not merged and (last_subject or last_preview):
        merged = [
            {
                "graph_message_id": None,
                "direction": "outbound",
                "subject": last_subject,
                "body_preview": last_preview,
                "conversation_id": None,
                "sent_datetime": None,
                "has_attachments": False,
            }
        ]
        data_source = "outlook_list"

    stats = compute_exchange_stats(merged, data_source=data_source)
    _ = full_name, company_name  # used by caller for AI prompt
    return stats, merged


def serialize_exchange_messages(messages: list[dict], *, limit: int = 5) -> list[dict]:
    items: list[dict] = []
    for message in messages[:limit]:
        sent_dt = message.get("sent_datetime")
        items.append(
            {
                "subject": message.get("subject"),
                "body_preview": message.get("body_preview"),
                "sent_datetime": sent_dt.isoformat() if sent_dt else None,
                "direction": message.get("direction") or "unknown",
                "outlook_weblink": message.get("outlook_weblink"),
                "has_attachments": bool(message.get("has_attachments")),
            }
        )
    return items
