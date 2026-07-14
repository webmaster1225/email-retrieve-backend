from __future__ import annotations

import re
from datetime import datetime
from html import unescape

from anthropic import Anthropic
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.services.exchange_service import build_exchange_prompt_context, gather_exchange_data
from app.services.graph_client import GraphClient, GraphAuthError
from app.services.text_utils import normalize_email

MAX_MESSAGES_FOR_AI = 20
MAX_BODY_CHARS = 4000


class AIServiceError(Exception):
    pass


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _get_client() -> Anthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise AIServiceError("ANTHROPIC_API_KEY is not configured in .env")
    return Anthropic(api_key=settings.anthropic_api_key)


def _get_contact(db: Session, contact_id: str) -> Contact:
    contact = (
        db.query(Contact)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.id == contact_id)
        .one_or_none()
    )
    if not contact:
        raise AIServiceError("Contact not found")
    return contact


def _contact_messages(db: Session, contact_id: str) -> list[EmailMessage]:
    return (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id)
        .order_by(EmailMessage.sent_datetime.desc())
        .limit(MAX_MESSAGES_FOR_AI)
        .all()
    )


def _needs_refresh(context: ContactContext | None, contact: Contact, field: str) -> bool:
    if not context:
        return True
    generated_at = context.ai_summary_generated_at
    if not getattr(context, field, None):
        return True
    if not generated_at or not contact.last_contacted_at:
        return False
    return contact.last_contacted_at > generated_at


def build_metadata_context(contact: Contact, messages: list[EmailMessage]) -> str:
    context = contact.context
    lines = [
        f"Contact: {contact.full_name} <{contact.primary_email}>",
        f"Company: {contact.company_name or 'Unknown'} ({contact.company_domain or 'n/a'})",
        f"First contacted: {contact.first_contacted_at}",
        f"Last contacted: {contact.last_contacted_at}",
        f"Email count: {contact.email_count}, Thread count: {contact.thread_count}",
        f"Fundraising score: {contact.fundraising_relevance_score} ({contact.fundraising_relevance_tier})",
        f"Detected topics: {', '.join(context.detected_topics or []) if context else 'none'}",
        f"Auto context: {context.auto_context_detailed if context else 'n/a'}",
        "",
        "Sent email history (newest first):",
    ]
    for msg in messages:
        lines.append(
            f"- [{msg.sent_datetime:%Y-%m-%d}] Subject: {msg.subject or '(no subject)'}\n"
            f"  Preview: {msg.body_preview or '(empty)'}"
        )
    return "\n".join(lines)


async def build_full_context(db: Session, contact_id: str) -> str:
    contact = _get_contact(db, contact_id)
    messages = _contact_messages(db, contact_id)
    base = build_metadata_context(contact, messages)

    graph = GraphClient(db)
    try:
        graph.ensure_access_token()
    except GraphAuthError as exc:
        raise AIServiceError(str(exc)) from exc

    body_sections: list[str] = []
    for msg in messages[:10]:
        try:
            body_data = await graph.fetch_message_body(msg.graph_message_id)
            body_content = body_data.get("body", {})
            raw = body_content.get("content", "")
            if body_content.get("contentType") == "html":
                raw = strip_html(raw)
            else:
                raw = raw.strip()
            if raw:
                truncated = raw[:MAX_BODY_CHARS]
                body_sections.append(
                    f"--- Full body [{msg.sent_datetime:%Y-%m-%d}] {msg.subject} ---\n{truncated}"
                )
        except Exception:
            continue

    if body_sections:
        return base + "\n\nFull message bodies (truncated):\n" + "\n\n".join(body_sections)
    return base


def _call_anthropic(system: str, user_prompt: str) -> str:
    settings = get_settings()
    client = _get_client()
    fallbacks = [
        settings.anthropic_model,
        "claude-haiku-4-5",
        "claude-sonnet-4-20250514",
        "claude-3-haiku-20240307",
    ]
    seen: set[str] = set()
    last_error: Exception | None = None
    for model in fallbacks:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1500,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            parts = [block.text for block in response.content if block.type == "text"]
            return "\n".join(parts).strip()
        except Exception as exc:
            last_error = exc
            continue
    raise AIServiceError(f"Anthropic API failed for all models: {last_error}")


def _ensure_context_row(db: Session, contact: Contact) -> ContactContext:
    if contact.context:
        return contact.context
    ctx = ContactContext(contact_id=contact.id)
    db.add(ctx)
    db.flush()
    contact.context = ctx
    return ctx


async def generate_summary(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_summary and not _needs_refresh(ctx, contact, "ai_summary"):
        return {"summary": ctx.ai_summary, "cached": True, "generated_at": ctx.ai_summary_generated_at}

    messages = _contact_messages(db, contact_id)
    prompt_context = build_metadata_context(contact, messages)
    summary = _call_anthropic(
        "You are a relationship intelligence assistant for Edge Investing / Galaxy Pharma fundraising and business development.",
        f"""Based on sent email metadata below, write a concise relationship summary covering:
- Who is this person and what company are they associated with?
- Why do we know them?
- What was discussed (best inference from subjects/previews)?
- Last meaningful status
- Useful for: fundraising, pharma/healthcare, board, networking, or other?
- Suggested next action

Keep it practical and under 300 words. If information is thin, say so clearly.

{prompt_context}""",
    )
    ctx.ai_summary = summary
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"summary": summary, "cached": False, "generated_at": ctx.ai_summary_generated_at}


async def generate_follow_up(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_follow_up_draft and not _needs_refresh(ctx, contact, "ai_follow_up_draft"):
        return {"draft": ctx.ai_follow_up_draft, "cached": True, "generated_at": ctx.ai_summary_generated_at}

    messages = _contact_messages(db, contact_id)
    prompt_context = build_metadata_context(contact, messages)
    if ctx.ai_summary:
        prompt_context += f"\n\nExisting AI summary:\n{ctx.ai_summary}"

    draft = _call_anthropic(
        "You write professional, warm follow-up emails for Edge Investing / Galaxy Pharma.",
        f"""Draft a short follow-up email to this contact based on the relationship history below.
- Professional but personable tone
- Reference the last conversation naturally
- Clear call to action (call, meeting, or next step)
- Under 150 words
- Do not invent specific facts not supported by the data
- Sign off as "Best regards" without a name (user will add signature)

Return only the email body (Subject line on first line as "Subject: ...").

{prompt_context}""",
    )
    ctx.ai_follow_up_draft = draft
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"draft": draft, "cached": False, "generated_at": ctx.ai_summary_generated_at}


async def classify_contact(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_contact_classification and not _needs_refresh(ctx, contact, "ai_contact_classification"):
        return {"classification": ctx.ai_contact_classification, "cached": True}

    messages = _contact_messages(db, contact_id)
    prompt_context = build_metadata_context(contact, messages)
    raw = _call_anthropic(
        "You classify business contacts for a healthcare investment firm.",
        f"""Classify this contact. Reply in this exact JSON format only (no markdown):
{{"contact_type": "investor|family_office|pharma|healthcare|advisor|vendor|legal|board|intro|other", "confidence": "high|medium|low", "reason": "one sentence"}}

{prompt_context}""",
    )
    import json

    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        classification = json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        classification = {"contact_type": "other", "confidence": "low", "reason": raw[:200]}

    ctx.ai_contact_classification = classification
    if classification.get("contact_type"):
        contact.contact_type = classification["contact_type"]
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"classification": classification, "cached": False}


async def summarize_threads(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_summary and ctx.ai_model_used and ctx.ai_model_used.endswith("-threads"):
        if not _needs_refresh(ctx, contact, "ai_summary"):
            return {"summary": ctx.ai_summary, "cached": True, "generated_at": ctx.ai_summary_generated_at}

    full_context = await build_full_context(db, contact_id)
    summary = _call_anthropic(
        "You summarize email thread history for fundraising and BD relationship management.",
        f"""Provide a detailed thread-by-thread summary of the relationship with this contact.
Include:
- Each major conversation theme
- Key decisions or open items
- Relationship trajectory over time
- Fundraising / pharma / BD relevance
- Recommended next action

Use full message content where available. Under 500 words.

{full_context}""",
    )
    ctx.ai_summary = summary
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model + "-threads"
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"summary": summary, "cached": False, "generated_at": ctx.ai_summary_generated_at}


def _parse_seniority_json(raw: str) -> dict:
    import json

    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        data = json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        data = {
            "title": None,
            "seniority_level": "unknown",
            "is_senior": False,
            "confidence": "low",
            "reason": raw[:200],
        }
    data.setdefault("title", None)
    data.setdefault("seniority_level", "unknown")
    data.setdefault("is_senior", False)
    data.setdefault("confidence", "low")
    data.setdefault("reason", "")
    return data


def _build_outlook_context(
    *,
    email: str,
    full_name: str | None,
    company_name: str | None,
    last_subject: str | None,
    last_preview: str | None,
) -> str:
    lines = [
        f"Contact: {full_name or 'Unknown'} <{email}>",
        f"Company: {company_name or 'Unknown'}",
        "",
        "Latest sent email:",
        f"- Subject: {last_subject or '(no subject)'}",
        f"  Preview: {last_preview or '(empty)'}",
    ]
    return "\n".join(lines)


def _infer_seniority(prompt_context: str) -> dict:
    raw = _call_anthropic(
        "You infer job titles and seniority from business email metadata for fundraising relationship management.",
        f"""Infer this contact's likely job title and seniority from the information below.
Look for signals in their name, email signature patterns, subject lines, email previews, and company context.
Examples of senior titles: CEO, Founder, Co-Founder, Managing Director, Partner, Principal, Executive Director, VP, SVP, EVP, President, Chairman, Board Member, General Partner.

Reply in this exact JSON format only (no markdown):
{{"title": "best guess title or null", "seniority_level": "c_suite|partner|executive|director|manager|individual_contributor|unknown", "is_senior": true|false, "confidence": "high|medium|low", "reason": "one sentence"}}

is_senior should be true for C-suite, founders, partners, managing directors, executives, VPs and similar decision-makers.
If there is not enough information, set title to null, seniority_level to "unknown", is_senior to false, and confidence to "low".

{prompt_context}""",
    )
    return _parse_seniority_json(raw)


async def detect_seniority(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_seniority and not _needs_refresh(ctx, contact, "ai_seniority"):
        return {"seniority": ctx.ai_seniority, "cached": True, "generated_at": ctx.ai_summary_generated_at}

    messages = _contact_messages(db, contact_id)
    prompt_context = build_metadata_context(contact, messages)
    seniority = _infer_seniority(prompt_context)
    ctx.ai_seniority = seniority
    if seniority.get("title"):
        ctx.detected_role = seniority["title"]
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model + "-seniority"
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {"seniority": seniority, "cached": False, "generated_at": ctx.ai_summary_generated_at}


async def detect_seniority_for_outlook(
    db: Session,
    email: str,
    *,
    full_name: str | None = None,
    company_name: str | None = None,
    last_subject: str | None = None,
    last_preview: str | None = None,
    force: bool = False,
) -> dict:
    local = (
        db.query(Contact)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.primary_email == email)
        .one_or_none()
    )
    if local and local.email_count > 0:
        return await detect_seniority(db, local.id, force=force)

    prompt_context = _build_outlook_context(
        email=email,
        full_name=full_name,
        company_name=company_name,
        last_subject=last_subject,
        last_preview=last_preview,
    )
    seniority = _infer_seniority(prompt_context)
    return {"seniority": seniority, "cached": False, "generated_at": None}


def _relationship_needs_refresh(ctx: ContactContext | None, contact: Contact) -> bool:
    if not ctx or not ctx.ai_relationship_analysis:
        return True
    generated_raw = ctx.ai_relationship_analysis.get("generated_at")
    if not generated_raw or not contact.last_contacted_at:
        return False
    try:
        generated_at = datetime.fromisoformat(str(generated_raw))
    except ValueError:
        return True
    return contact.last_contacted_at > generated_at


def _parse_relationship_json(raw: str) -> dict:
    import json

    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        data = json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        data = {
            "conversation_pattern": "unknown",
            "conversation_depth": "unknown",
            "depth_score": 1,
            "business_usefulness": {},
            "primary_value": "unknown",
            "summary": raw[:300],
            "confidence": "low",
            "reason": "Could not parse AI response",
        }
    data.setdefault("conversation_pattern", "unknown")
    data.setdefault("conversation_depth", "unknown")
    data.setdefault("depth_score", 1)
    data.setdefault("business_usefulness", {})
    data.setdefault("primary_value", "unknown")
    data.setdefault("summary", "")
    data.setdefault("confidence", "low")
    data.setdefault("reason", "")
    return data


def _infer_relationship(prompt_context: str) -> dict:
    raw = _call_anthropic(
        "You analyze email relationship patterns for Edge Investing / Galaxy Pharma business development.",
        f"""Analyze the email exchange history below and assess the relationship.

Reply in this exact JSON format only (no markdown):
{{
  "conversation_pattern": "two_way|mostly_outbound|mostly_inbound|one_off|unknown",
  "conversation_depth": "deep|moderate|shallow|minimal|unknown",
  "depth_score": 1,
  "business_usefulness": {{
    "business_development": "high|medium|low|none",
    "board_opportunities": "high|medium|low|none",
    "ma": "high|medium|low|none",
    "investment": "high|medium|low|none",
    "fundraising": "high|medium|low|none",
    "strategic_introductions": "high|medium|low|none"
  }},
  "primary_value": "business_development|board_opportunities|ma|investment|fundraising|strategic_introductions|limited|unknown",
  "summary": "2-3 sentences on relationship quality and opportunity",
  "confidence": "high|medium|low",
  "reason": "one sentence citing key evidence"
}}

Guidance:
- two_way means meaningful back-and-forth, not just us emailing them
- depth should reflect substance of topics discussed, not just message count
- Base usefulness ratings on actual email content and relationship signals
- If data is thin, use low confidence and unknown/minimal where appropriate

{prompt_context}""",
    )
    return _parse_relationship_json(raw)


async def analyze_relationship(db: Session, contact_id: str, *, force: bool = False) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)
    if not force and ctx.ai_relationship_analysis and not _relationship_needs_refresh(ctx, contact):
        cached = ctx.ai_relationship_analysis
        return {
            "stats": cached.get("stats", {}),
            "analysis": cached.get("analysis", {}),
            "cached": True,
            "generated_at": cached.get("generated_at"),
        }

    stats, messages = await gather_exchange_data(
        db,
        contact.primary_email,
        full_name=contact.full_name,
        company_name=contact.company_name,
    )
    prompt_context = build_exchange_prompt_context(
        contact_email=contact.primary_email,
        full_name=contact.full_name,
        company_name=contact.company_name,
        stats=stats,
        messages=messages,
    )
    analysis = _infer_relationship(prompt_context)
    generated_at = datetime.utcnow().isoformat()
    payload = {"generated_at": generated_at, "stats": stats, "analysis": analysis}
    ctx.ai_relationship_analysis = payload
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model + "-relationship"
    ctx.updated_at = datetime.utcnow()
    db.commit()
    return {
        "stats": stats,
        "analysis": analysis,
        "cached": False,
        "generated_at": generated_at,
    }


async def analyze_relationship_for_outlook(
    db: Session,
    email: str,
    *,
    full_name: str | None = None,
    company_name: str | None = None,
    last_subject: str | None = None,
    last_preview: str | None = None,
    force: bool = False,
) -> dict:
    normalized = normalize_email(email)
    if not normalized:
        raise AIServiceError("Invalid email address")

    local = (
        db.query(Contact)
        .options(joinedload(Contact.context))
        .filter(Contact.primary_email == normalized)
        .one_or_none()
    )
    if local:
        return await analyze_relationship(db, local.id, force=force)

    stats, messages = await gather_exchange_data(
        db,
        normalized,
        full_name=full_name,
        company_name=company_name,
        last_subject=last_subject,
        last_preview=last_preview,
    )
    prompt_context = build_exchange_prompt_context(
        contact_email=normalized,
        full_name=full_name,
        company_name=company_name,
        stats=stats,
        messages=messages,
    )
    analysis = _infer_relationship(prompt_context)
    return {
        "stats": stats,
        "analysis": analysis,
        "cached": False,
        "generated_at": None,
    }


def ai_status(db: Session, contact_id: str) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = contact.context
    return {
        "has_summary": bool(ctx and ctx.ai_summary),
        "has_follow_up": bool(ctx and ctx.ai_follow_up_draft),
        "has_classification": bool(ctx and ctx.ai_contact_classification),
        "has_seniority": bool(ctx and ctx.ai_seniority),
        "has_relationship_analysis": bool(ctx and ctx.ai_relationship_analysis),
        "summary_generated_at": ctx.ai_summary_generated_at if ctx else None,
        "model_used": ctx.ai_model_used if ctx else None,
        "needs_refresh": _needs_refresh(ctx, contact, "ai_summary") if ctx else True,
    }
