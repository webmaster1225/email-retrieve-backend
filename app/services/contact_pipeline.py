from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import ConversationThread, EmailMessage
from app.services.company_resolver import best_display_name, resolve_company
from app.services.scorer import compute_fundraising_score, detect_topics, infer_contact_type, score_to_tier
from app.services.text_utils import is_noise_email, is_trivial_preview, normalize_email, parse_display_name


def upsert_recipient_link(
    db: Session,
    *,
    message: EmailMessage,
    email: str,
    display_name: str | None,
    recipient_type: str,
    contact_cache: dict[str, Contact] | None = None,
) -> Contact | None:
    if not email or is_noise_email(email):
        return None

    company_name, company_domain, is_internal, is_personal = resolve_company(email, display_name)
    contact = contact_cache.get(email) if contact_cache is not None else None
    if contact is None:
        contact = db.query(Contact).filter(Contact.primary_email == email).one_or_none()

    if contact is None:
        contact = Contact(
            primary_email=email,
            full_name=best_display_name(None, display_name, email),
            company_name=company_name,
            company_domain=company_domain,
            is_internal=is_internal,
            is_personal_email=is_personal,
            is_excluded=is_noise_email(email),
            exclusion_reason="noise_email" if is_noise_email(email) else None,
        )
        db.add(contact)
        db.flush()
    else:
        contact.full_name = best_display_name(contact.full_name, display_name, email)
        if not contact.company_name or contact.company_name == "Personal email / Unknown company":
            contact.company_name = company_name
            contact.company_domain = company_domain
        contact.is_internal = is_internal
        contact.is_personal_email = is_personal

    if contact_cache is not None:
        contact_cache[email] = contact

    existing_link = (
        db.query(ContactEmailLink)
        .filter(
            ContactEmailLink.contact_id == contact.id,
            ContactEmailLink.email_message_id == message.id,
            ContactEmailLink.recipient_type == recipient_type,
        )
        .one_or_none()
    )
    if existing_link is None:
        db.add(
            ContactEmailLink(
                contact_id=contact.id,
                email_message_id=message.id,
                recipient_type=recipient_type,
            )
        )
    return contact


def process_message_recipients(
    db: Session,
    message: EmailMessage,
    *,
    contact_cache: dict[str, Contact] | None = None,
) -> list[str]:
    touched_contact_ids: list[str] = []
    seen: set[tuple[str, str]] = set()
    recipient_groups = [
        (message.raw_to or [], "to"),
        (message.raw_cc or [], "cc"),
        (message.raw_bcc or [], "bcc"),
    ]
    for recipients, recipient_type in recipient_groups:
        for recipient in recipients:
            display_name, email = parse_display_name(recipient)
            email = normalize_email(email)
            if not email:
                continue
            key = (email, recipient_type)
            if key in seen:
                continue
            seen.add(key)
            contact = upsert_recipient_link(
                db,
                message=message,
                email=email,
                display_name=display_name,
                recipient_type=recipient_type,
                contact_cache=contact_cache,
            )
            if contact and contact.id not in touched_contact_ids:
                touched_contact_ids.append(contact.id)
    return touched_contact_ids


def process_inbound_sender(
    db: Session,
    message: EmailMessage,
    *,
    contact_cache: dict[str, Contact] | None = None,
) -> list[str]:
    touched: list[str] = []
    if not message.sender_email or is_noise_email(message.sender_email):
        return touched
    display_name, email = parse_display_name(message.raw_from or {})
    if not email:
        email = message.sender_email
    contact = upsert_recipient_link(
        db,
        message=message,
        email=email,
        display_name=display_name,
        recipient_type="from",
        contact_cache=contact_cache,
    )
    if contact and contact.id not in touched:
        touched.append(contact.id)
    return touched


def _update_reply_status(contact: Contact, messages: list[EmailMessage]) -> None:
    outbound = [m for m in messages if (m.direction or "outbound") == "outbound"]
    inbound = [m for m in messages if m.direction == "inbound"]

    last_outbound = max(outbound, key=lambda m: m.sent_datetime) if outbound else None
    last_inbound = max(inbound, key=lambda m: m.sent_datetime) if inbound else None

    contact.last_outbound_at = last_outbound.sent_datetime if last_outbound else None
    contact.last_inbound_at = last_inbound.sent_datetime if last_inbound else None

    if last_outbound:
        contact.last_contacted_at = last_outbound.sent_datetime

    if last_outbound and (not last_inbound or last_inbound.sent_datetime < last_outbound.sent_datetime):
        contact.awaiting_reply = True
        now = datetime.now(timezone.utc)
        last = last_outbound.sent_datetime
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        contact.days_since_outreach = max(0, (now - last).days)
    else:
        contact.awaiting_reply = False
        contact.days_since_outreach = None


def rebuild_contact_aggregates(db: Session, contact_ids: list[str] | None = None) -> int:
    """Rebuild contact stats/threads/scores with batched queries (no per-thread N+1)."""
    query = db.query(Contact)
    if contact_ids:
        if not contact_ids:
            return 0
        query = query.filter(Contact.id.in_(contact_ids))
    contacts = query.all()
    if not contacts:
        return 0

    ids = [c.id for c in contacts]
    contact_by_id = {c.id: c for c in contacts}

    # One query: all messages for these contacts
    rows = (
        db.query(ContactEmailLink.contact_id, EmailMessage)
        .join(EmailMessage, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id.in_(ids))
        .order_by(EmailMessage.sent_datetime.asc())
        .all()
    )
    messages_by_contact: dict[str, list[EmailMessage]] = defaultdict(list)
    seen_msg: dict[str, set[str]] = defaultdict(set)
    for contact_id, message in rows:
        if message.id in seen_msg[contact_id]:
            continue
        seen_msg[contact_id].add(message.id)
        messages_by_contact[contact_id].append(message)

    # Existing threads for these contacts (one query)
    existing_threads = (
        db.query(ConversationThread)
        .filter(ConversationThread.contact_id.in_(ids))
        .all()
    )
    thread_map: dict[tuple[str, str], ConversationThread] = {
        (t.contact_id, t.conversation_id): t for t in existing_threads
    }

    # Existing contexts (one query)
    existing_contexts = (
        db.query(ContactContext).filter(ContactContext.contact_id.in_(ids)).all()
    )
    context_map = {ctx.contact_id: ctx for ctx in existing_contexts}

    updated = 0
    for contact_id, messages in messages_by_contact.items():
        contact = contact_by_id[contact_id]
        if not messages:
            continue

        outbound_messages = [m for m in messages if (m.direction or "outbound") == "outbound"]
        if not outbound_messages:
            outbound_messages = messages

        contact.first_contacted_at = outbound_messages[0].sent_datetime
        contact.last_contacted_at = outbound_messages[-1].sent_datetime
        contact.email_count = len(outbound_messages)
        _update_reply_status(contact, messages)

        # Group messages by conversation in memory
        by_conversation: dict[str, list[EmailMessage]] = defaultdict(list)
        for message in messages:
            if message.conversation_id:
                by_conversation[message.conversation_id].append(message)

        contact.thread_count = len(by_conversation)

        for conversation_id, thread_messages in by_conversation.items():
            thread_messages_sorted = sorted(thread_messages, key=lambda m: m.sent_datetime)
            latest = thread_messages_sorted[-1]
            first_at = thread_messages_sorted[0].sent_datetime
            last_at = latest.sent_datetime
            subjects = sorted({m.subject or "" for m in thread_messages_sorted if m.subject})
            key = (contact.id, conversation_id)
            thread = thread_map.get(key)
            if thread is None:
                thread = ConversationThread(contact_id=contact.id, conversation_id=conversation_id)
                db.add(thread)
                thread_map[key] = thread
            thread.first_message_at = first_at
            thread.last_message_at = last_at
            thread.message_count = len(thread_messages_sorted)
            thread.latest_subject = latest.subject
            thread.latest_preview = latest.body_preview
            thread.latest_outlook_weblink = latest.outlook_weblink
            thread.subjects_all = subjects
            thread.detected_keywords = detect_topics(
                subjects, [m.body_preview or "" for m in thread_messages_sorted]
            )
            thread.updated_at = datetime.utcnow()

        subjects = [m.subject or "" for m in outbound_messages]
        previews = [m.body_preview or "" for m in outbound_messages]
        meaningful = [p for p in reversed(previews) if not is_trivial_preview(p)][:3]
        topics = detect_topics(subjects, previews)
        has_attachments = any(m.has_attachments for m in outbound_messages)

        score, breakdown = compute_fundraising_score(
            company_domain=contact.company_domain,
            company_name=contact.company_name,
            subjects=subjects,
            previews=previews,
            email_count=contact.email_count,
            last_contacted_at=contact.last_contacted_at,
            has_attachments=has_attachments,
            is_internal=contact.is_internal,
            is_personal_email=contact.is_personal_email,
            is_excluded=contact.is_excluded,
        )
        contact.fundraising_relevance_score = score
        contact.fundraising_relevance_tier = score_to_tier(score)
        contact.score_breakdown = breakdown
        contact.relationship_score = min(contact.email_count * 5 + contact.thread_count * 3, 100)
        contact.contact_type = infer_contact_type(
            topics,
            score,
            f"{contact.company_domain or ''} {contact.company_name or ''}".lower(),
        )

        latest = outbound_messages[-1]
        latest_meaningful = next(
            (m for m in reversed(outbound_messages) if not is_trivial_preview(m.body_preview)), latest
        )
        # Denormalized list fields — avoids a join on every contacts page load
        contact.last_subject = latest.subject
        contact.last_preview = latest.body_preview
        contact.latest_outlook_weblink = latest.outlook_weblink

        context = context_map.get(contact.id)
        if context is None:
            context = ContactContext(contact_id=contact.id)
            db.add(context)
            context_map[contact.id] = context

        topic_text = ", ".join(topics) if topics else "general correspondence"
        context.auto_context_short = (
            f"Contacted {contact.email_count} times across {contact.thread_count} threads. "
            f"Topics: {topic_text}. Last: {contact.last_contacted_at.strftime('%b %Y') if contact.last_contacted_at else 'n/a'}."
        )
        subject_sample = "; ".join(list(dict.fromkeys(s for s in subjects if s))[-5:])
        context.auto_context_detailed = (
            f"Contacted {contact.email_count} times across {contact.thread_count} thread(s). "
            f"First contacted {contact.first_contacted_at.strftime('%b %Y') if contact.first_contacted_at else 'n/a'}. "
            f"Last contacted {contact.last_contacted_at.strftime('%b %Y') if contact.last_contacted_at else 'n/a'}. "
            f"Main topics appear to be {topic_text}. "
            f"Recent subjects: {subject_sample or latest.subject or 'n/a'}. "
            f"Fundraising relevance: {contact.fundraising_relevance_tier} ({score})."
        )
        context.last_meaningful_email_preview = latest_meaningful.body_preview
        context.last_meaningful_message_id = latest_meaningful.id
        context.detected_topics = topics
        context.meaningful_previews = meaningful
        context.updated_at = datetime.utcnow()
        updated += 1

    db.commit()
    return updated
