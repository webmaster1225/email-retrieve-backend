from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session, joinedload

from app.models.outreach import EmailDraft, OutreachPrompt
from app.services.ai_service import _call_anthropic, build_metadata_context, _contact_messages, _get_contact
from app.services.graph_client import GraphAuthError, GraphClient

DEFAULT_SYSTEM_PROMPT = (
    "You write professional, warm fundraising outreach emails for Edge Investing / Galaxy Pharma. "
    "Personalize based on prior correspondence. Never invent facts not supported by the contact data."
)

DEFAULT_USER_PROMPT_TEMPLATE = """Draft a fundraising outreach email to this contact.

Requirements:
- Professional, warm, personalized tone
- Reference prior correspondence naturally if any exists
- Clear call to action (call or meeting about a funding opportunity)
- Do not invent specific facts not supported by the data below
- Under 200 words for the body
- Sign off as "Best regards" without a name (the user will add their signature)

{custom_instructions_block}

Return ONLY in this format:
Subject: <subject line>

<body paragraphs>

Contact context:
{context}"""


class OutreachError(Exception):
    pass


def _parse_draft_response(text: str) -> tuple[str, str]:
    lines = text.strip().splitlines()
    subject = ""
    body_lines: list[str] = []
    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line.split(":", 1)[1].strip()
            body_lines = lines[i + 1 :]
            break
    if not subject and lines:
        subject = lines[0].strip()
        body_lines = lines[1:]
    body = "\n".join(body_lines).strip()
    return subject, body


def get_or_create_prompt(db: Session) -> OutreachPrompt:
    row = db.query(OutreachPrompt).filter(OutreachPrompt.id == "default").one_or_none()
    if row:
        return row
    row = OutreachPrompt(
        id="default",
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        user_prompt_template=DEFAULT_USER_PROMPT_TEMPLATE,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_prompt_config(db: Session) -> dict:
    row = get_or_create_prompt(db)
    return {
        "system_prompt": row.system_prompt,
        "user_prompt_template": row.user_prompt_template,
        "updated_at": row.updated_at,
    }


def update_prompt_config(db: Session, *, system_prompt: str | None, user_prompt_template: str | None) -> dict:
    row = get_or_create_prompt(db)
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if user_prompt_template is not None:
        row.user_prompt_template = user_prompt_template
    row.updated_at = datetime.utcnow()
    db.commit()
    return get_prompt_config(db)


def build_user_prompt(
    template: str,
    context: str,
    custom_instructions: str | None,
) -> str:
    block = ""
    if custom_instructions and custom_instructions.strip():
        block = f"Additional instructions from the user:\n{custom_instructions.strip()}\n"
    return template.replace("{custom_instructions_block}", block).replace("{context}", context)


async def generate_draft_for_contact(
    db: Session,
    contact_id: str,
    *,
    custom_instructions: str | None = None,
) -> EmailDraft:
    contact = _get_contact(db, contact_id)
    if contact.review_status != "approved":
        raise OutreachError(f"Contact {contact.primary_email} is not approved for outreach")

    prompt_row = get_or_create_prompt(db)
    messages = _contact_messages(db, contact_id)
    context = build_metadata_context(contact, messages)
    user_prompt = build_user_prompt(prompt_row.user_prompt_template, context, custom_instructions)

    raw = _call_anthropic(prompt_row.system_prompt, user_prompt)
    subject, body = _parse_draft_response(raw)

    existing = (
        db.query(EmailDraft)
        .filter(EmailDraft.contact_id == contact_id, EmailDraft.status.in_(["draft", "approved"]))
        .order_by(EmailDraft.created_at.desc())
        .first()
    )
    draft = existing or EmailDraft(contact_id=contact_id)
    draft.subject = subject
    draft.body = body
    draft.status = "draft"
    draft.custom_instructions = custom_instructions
    draft.system_prompt = prompt_row.system_prompt
    draft.user_prompt = user_prompt
    draft.error_message = None
    draft.updated_at = datetime.utcnow()
    if not existing:
        db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


async def generate_drafts_bulk(
    db: Session,
    contact_ids: list[str],
    *,
    custom_instructions: str | None = None,
) -> list[dict]:
    results: list[dict] = []
    for contact_id in contact_ids:
        try:
            draft = await generate_draft_for_contact(db, contact_id, custom_instructions=custom_instructions)
            results.append({"contact_id": contact_id, "draft_id": draft.id, "status": "ok"})
        except Exception as exc:
            results.append({"contact_id": contact_id, "status": "error", "error": str(exc)})
    return results


def list_drafts(db: Session, status: str | None = None) -> list[EmailDraft]:
    query = (
        db.query(EmailDraft)
        .options(joinedload(EmailDraft.contact))
        .order_by(EmailDraft.updated_at.desc())
    )
    if status:
        query = query.filter(EmailDraft.status == status)
    return query.all()


def draft_to_dict(draft: EmailDraft) -> dict:
    contact = draft.contact
    return {
        "id": draft.id,
        "contact_id": draft.contact_id,
        "contact_name": contact.full_name if contact else None,
        "contact_email": contact.primary_email if contact else None,
        "list_number": contact.list_number if contact else None,
        "subject": draft.subject,
        "body": draft.body,
        "status": draft.status,
        "custom_instructions": draft.custom_instructions,
        "system_prompt": draft.system_prompt,
        "user_prompt": draft.user_prompt,
        "error_message": draft.error_message,
        "sent_at": draft.sent_at,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }


def update_draft(db: Session, draft_id: str, *, subject: str | None, body: str | None, status: str | None) -> EmailDraft:
    draft = db.query(EmailDraft).filter(EmailDraft.id == draft_id).one_or_none()
    if not draft:
        raise OutreachError("Draft not found")
    if subject is not None:
        draft.subject = subject
    if body is not None:
        draft.body = body
    if status is not None:
        draft.status = status
    draft.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(draft)
    return draft


async def send_draft(db: Session, draft_id: str) -> EmailDraft:
    draft = (
        db.query(EmailDraft)
        .options(joinedload(EmailDraft.contact))
        .filter(EmailDraft.id == draft_id)
        .one_or_none()
    )
    if not draft:
        raise OutreachError("Draft not found")
    if draft.status == "sent":
        raise OutreachError("Draft already sent")
    contact = draft.contact
    if not contact:
        raise OutreachError("Contact not found")
    if not draft.subject or not draft.body:
        raise OutreachError("Draft is missing subject or body")

    graph = GraphClient(db)
    try:
        await graph.send_mail(
            to_email=contact.primary_email,
            to_name=contact.full_name,
            subject=draft.subject,
            body=draft.body,
        )
    except GraphAuthError as exc:
        draft.error_message = str(exc)
        draft.updated_at = datetime.utcnow()
        db.commit()
        raise OutreachError(str(exc)) from exc

    draft.status = "sent"
    draft.sent_at = datetime.utcnow()
    draft.error_message = None
    draft.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(draft)
    return draft


async def send_approved_drafts(db: Session) -> list[dict]:
    drafts = db.query(EmailDraft).filter(EmailDraft.status == "approved").all()
    results: list[dict] = []
    for draft in drafts:
        try:
            await send_draft(db, draft.id)
            results.append({"draft_id": draft.id, "status": "sent"})
        except Exception as exc:
            results.append({"draft_id": draft.id, "status": "error", "error": str(exc)})
    return results
