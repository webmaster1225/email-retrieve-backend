from __future__ import annotations

from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.models.sync import SyncRun
from app.schemas import ContactDetail, ContactListItem, ContactUpdate, StatsOut
from app.services.company_resolver import best_display_name, resolve_company
from app.services.graph_client import GraphAuthError, GraphClient
from app.services.sent_contacts import extract_contacts_from_messages
from app.services.text_utils import normalize_email

router = APIRouter(prefix="/contacts", tags=["contacts"])

REVIEW_STATUSES = {"pending", "approved", "denied"}


def _hydrate_list_item(contact: Contact) -> ContactListItem:
    context: ContactContext | None = contact.context
    latest_message = None
    if contact.email_links:
        latest_message = max(contact.email_links, key=lambda link: link.message.sent_datetime).message
    return ContactListItem(
        id=contact.id,
        list_number=contact.list_number,
        full_name=contact.full_name,
        primary_email=contact.primary_email,
        company_name=contact.company_name,
        company_domain=contact.company_domain,
        first_contacted_at=contact.first_contacted_at,
        last_contacted_at=contact.last_contacted_at,
        email_count=contact.email_count,
        thread_count=contact.thread_count,
        fundraising_relevance_score=contact.fundraising_relevance_score,
        fundraising_relevance_tier=contact.fundraising_relevance_tier,
        contact_type=contact.contact_type,
        status=contact.status,
        review_status=contact.review_status or "pending",
        notes=contact.notes,
        awaiting_reply=contact.awaiting_reply,
        days_since_outreach=contact.days_since_outreach,
        last_inbound_at=contact.last_inbound_at,
        outreach_relevance_score=getattr(contact, "outreach_relevance_score", 0) or 0,
        outreach_relevance_tier=getattr(contact, "outreach_relevance_tier", None),
        outreach_score_explanation=getattr(contact, "outreach_score_explanation", None),
        is_internal=contact.is_internal,
        is_personal_email=contact.is_personal_email,
        is_excluded=contact.is_excluded,
        auto_context_short=context.auto_context_short if context else None,
        detected_topics=context.detected_topics if context else None,
        detected_role=context.detected_role if context else None,
        ai_seniority=context.ai_seniority if context else None,
        ai_outreach_intelligence=context.ai_outreach_intelligence if context else None,
        last_subject=latest_message.subject if latest_message else None,
        last_preview=latest_message.body_preview if latest_message else None,
        latest_outlook_weblink=latest_message.outlook_weblink if latest_message else None,
        latest_message_id=latest_message.id if latest_message else None,
        has_ai_summary=bool(context and context.ai_summary),
        has_outreach_intelligence=bool(context and context.ai_outreach_intelligence),
    )


def _parse_exclude_emails(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _enrich_outlook_item(db: Session, item: dict, local_by_email: dict[str, Contact] | None = None) -> dict:
    email = item["primary_email"]
    local = local_by_email.get(email) if local_by_email is not None else None
    if local is None and local_by_email is None:
        local = (
            db.query(Contact)
            .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
            .filter(Contact.primary_email == email)
            .one_or_none()
        )

    enriched = {
        **item,
        "list_number": None,
        "local_contact_id": None,
        "email_count": item.get("email_count", 0),
        "thread_count": item.get("thread_count", 0),
        "fundraising_relevance_score": item.get("fundraising_relevance_score", 0),
        "fundraising_relevance_tier": item.get("fundraising_relevance_tier") or "low",
        "review_status": "pending",
        "detected_topics": item.get("detected_topics") or [],
    }

    if not local:
        return enriched

    hydrated = _hydrate_list_item(local).model_dump()
    enriched.update(
        {
            "local_contact_id": local.id,
            "list_number": hydrated["list_number"],
            "email_count": hydrated["email_count"],
            "thread_count": hydrated["thread_count"],
            "fundraising_relevance_score": hydrated["fundraising_relevance_score"],
            "fundraising_relevance_tier": hydrated["fundraising_relevance_tier"] or "low",
            "review_status": hydrated["review_status"],
            "detected_topics": hydrated["detected_topics"] or [],
            "detected_role": (local.context.detected_role if local.context else None),
            "ai_seniority": (local.context.ai_seniority if local.context else None),
            "ai_relationship_analysis": (
                local.context.ai_relationship_analysis if local.context else None
            ),
            "full_name": hydrated["full_name"] or item.get("full_name"),
            "company_name": hydrated["company_name"] or item.get("company_name"),
        }
    )
    for key in ("last_contacted_at", "last_subject", "last_preview", "latest_outlook_weblink"):
        if item.get(key):
            enriched[key] = item[key]
        elif hydrated.get(key):
            enriched[key] = hydrated[key]
    return enriched


def _enrich_outlook_items_batch(db: Session, items: list[dict]) -> list[dict]:
    if not items:
        return []
    emails = [item["primary_email"] for item in items]
    locals_list = (
        db.query(Contact)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.primary_email.in_(emails))
        .all()
    )
    by_email = {contact.primary_email: contact for contact in locals_list}
    return [_enrich_outlook_item(db, item, by_email) for item in items]


def _iso_or_none(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _local_contact_to_outlook(hydrated: dict) -> dict:
    return {
        "id": hydrated["primary_email"],
        "local_contact_id": hydrated["id"],
        "list_number": hydrated["list_number"],
        "full_name": hydrated["full_name"],
        "primary_email": hydrated["primary_email"],
        "company_name": hydrated["company_name"],
        "company_domain": hydrated["company_domain"],
        "last_contacted_at": _iso_or_none(hydrated.get("last_contacted_at")),
        "last_subject": hydrated.get("last_subject"),
        "last_preview": hydrated.get("last_preview"),
        "latest_message_id": hydrated.get("latest_message_id"),
        "latest_outlook_weblink": hydrated.get("latest_outlook_weblink"),
        "email_count": hydrated["email_count"],
        "thread_count": hydrated["thread_count"],
        "fundraising_relevance_score": hydrated["fundraising_relevance_score"],
        "fundraising_relevance_tier": hydrated["fundraising_relevance_tier"] or "low",
        "review_status": hydrated["review_status"],
        "detected_topics": hydrated.get("detected_topics") or [],
    }


def _parse_local_offset(next_link: str | None) -> int | None:
    if next_link and next_link.startswith("local:"):
        try:
            return int(next_link.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _list_outlook_from_local(
    db: Session,
    *,
    page_size: int,
    q: str | None,
    exclude_emails: set[str],
    offset: int,
) -> dict:
    query = (
        db.query(Contact)
        .outerjoin(ContactContext, ContactContext.contact_id == Contact.id)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.is_internal.is_(False), Contact.is_excluded.is_(False))
    )
    if exclude_emails:
        query = query.filter(~Contact.primary_email.in_(exclude_emails))
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(
                func.lower(Contact.full_name).like(like),
                func.lower(Contact.primary_email).like(like),
                func.lower(Contact.company_name).like(like),
                func.lower(Contact.company_domain).like(like),
            )
        )
    query = query.order_by(Contact.last_contacted_at.desc().nullslast(), Contact.full_name.asc())
    rows = query.offset(offset).limit(page_size + 1).all()
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    items = [_local_contact_to_outlook(_hydrate_list_item(contact).model_dump()) for contact in rows]
    next_cursor = f"local:{offset + len(items)}" if has_more else None
    return {"items": items, "next_link": next_cursor, "total": None, "source": "local"}


async def _list_outlook_from_graph(
    db: Session,
    *,
    page_size: int,
    q: str | None,
    exclude_emails: set[str],
    next_link: str | None,
    include_total: bool,
) -> dict:
    client = GraphClient(db)
    collected: list[dict] = []
    url = next_link
    message_next_link: str | None = None
    total_messages: int | None = None
    max_message_pages = 3

    if url is None and include_total:
        try:
            folder = await client.fetch_sent_items_folder()
            total_messages = folder.get("totalItemCount")
        except (httpx.TimeoutException, httpx.HTTPError):
            total_messages = None

    for _ in range(max_message_pages):
        page = await client.fetch_messages_page(url, top=50, newest_first=True)
        batch = extract_contacts_from_messages(
            page.get("value", []),
            exclude_emails=exclude_emails,
            q=q,
        )
        for email, contact in batch.items():
            if email in exclude_emails:
                continue
            collected.append(contact)
            exclude_emails.add(email)

        message_next_link = page.get("@odata.nextLink")
        if len(collected) >= page_size or not message_next_link:
            break
        url = message_next_link

    return {
        "items": _enrich_outlook_items_batch(db, collected[:page_size]),
        "next_link": message_next_link,
        "total": total_messages,
        "source": "graph",
    }


@router.get("/outlook")
async def list_outlook_contacts(
    db: Session = Depends(get_db),
    q: str | None = None,
    page_size: int = Query(50, ge=1, le=100),
    next_link: str | None = None,
    exclude_emails: str | None = None,
    include_total: bool = False,
    prefer_local: bool = True,
):
    """Derive contacts from Sent Items (Mail.Read) — paginated, optimized."""
    exclude = _parse_exclude_emails(exclude_emails)
    local_offset = _parse_local_offset(next_link)

    try:
        if local_offset is not None:
            return _list_outlook_from_local(
                db,
                page_size=page_size,
                q=q,
                exclude_emails=set(),
                offset=local_offset,
            )

        if prefer_local and next_link is None:
            has_local = (
                db.query(Contact.id)
                .filter(Contact.is_internal.is_(False), Contact.is_excluded.is_(False))
                .limit(1)
                .first()
            )
            if has_local:
                return _list_outlook_from_local(
                    db,
                    page_size=page_size,
                    q=q,
                    exclude_emails=exclude,
                    offset=0,
                )

        return await _list_outlook_from_graph(
            db,
            page_size=page_size,
            q=q,
            exclude_emails=exclude,
            next_link=next_link,
            include_total=include_total,
        )
    except GraphAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail="Microsoft Graph request timed out. Check your network or disable VPN/proxy.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Failed to reach Microsoft Graph. Check your network connection.",
        ) from exc


@router.patch("/by-email/{contact_email:path}", response_model=ContactDetail)
def update_contact_by_email(contact_email: str, payload: ContactUpdate, db: Session = Depends(get_db)):
    email = normalize_email(contact_email)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    contact = db.query(Contact).filter(Contact.primary_email == email).one_or_none()
    if not contact:
        company_name, company_domain, is_internal, is_personal = resolve_company(email)
        if is_internal:
            raise HTTPException(status_code=404, detail="Contact not found")
        contact = Contact(
            primary_email=email,
            full_name=best_display_name(None, None, email),
            company_name=company_name,
            company_domain=company_domain,
            is_internal=is_internal,
            is_personal_email=is_personal,
        )
        db.add(contact)
        db.flush()

    updates = payload.model_dump(exclude_unset=True)
    if "review_status" in updates and updates["review_status"] not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="review_status must be pending, approved, or denied")
    for field, value in updates.items():
        setattr(contact, field, value)
    contact.updated_at = datetime.utcnow()
    db.commit()
    return get_contact(contact.id, db)


@router.get("/outlook/{contact_email:path}")
async def get_outlook_contact(contact_email: str, db: Session = Depends(get_db)):
    email = normalize_email(contact_email)
    if not email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    local = db.query(Contact).filter(Contact.primary_email == email).one_or_none()
    if local:
        enriched = _enrich_outlook_item(db, {
            "id": email,
            "primary_email": email,
            "full_name": local.full_name,
            "company_name": local.company_name,
            "company_domain": local.company_domain,
        })
        detail = _hydrate_list_item(local).model_dump()
        detail.update({k: v for k, v in enriched.items() if v is not None})
        return detail

    company_name, company_domain, is_internal, _is_personal = resolve_company(email)
    if is_internal:
        raise HTTPException(status_code=404, detail="Contact not found")

    return {
        "id": email,
        "full_name": best_display_name(None, None, email),
        "primary_email": email,
        "company_name": company_name,
        "company_domain": company_domain,
        "last_contacted_at": None,
        "last_subject": None,
        "last_preview": None,
        "latest_message_id": None,
        "latest_outlook_weblink": None,
    }


@router.get("", response_model=dict)
def list_contacts(
    db: Session = Depends(get_db),
    q: str | None = None,
    fundraising_tier: str | None = None,
    exclude_internal: bool = True,
    exclude_personal: bool = False,
    exclude_noise: bool = True,
    email_count_min: int | None = None,
    keyword: str | None = None,
    only_investor: bool = False,
    review_status: str | None = None,
    not_replied_days: int | None = None,
    awaiting_reply_only: bool = False,
    sort: str = "last_contacted_at",
    order: str = "desc",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    query = (
        db.query(Contact)
        .outerjoin(ContactContext, ContactContext.contact_id == Contact.id)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
    )

    if exclude_noise:
        query = query.filter(Contact.is_excluded.is_(False))

    # Hide dev-only test seed contacts (graph_message_id starting with test-)
    has_real_message = (
        db.query(ContactEmailLink.id)
        .join(EmailMessage, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(
            ContactEmailLink.contact_id == Contact.id,
            ~EmailMessage.graph_message_id.like("test-%"),
        )
        .exists()
    )
    query = query.filter(has_real_message)

    if exclude_internal:
        query = query.filter(Contact.is_internal.is_(False))
    if exclude_personal:
        query = query.filter(Contact.is_personal_email.is_(False))
    if fundraising_tier:
        tiers = [t.strip() for t in fundraising_tier.split(",") if t.strip()]
        query = query.filter(Contact.fundraising_relevance_tier.in_(tiers))
    if email_count_min is not None:
        query = query.filter(Contact.email_count >= email_count_min)
    if only_investor:
        query = query.filter(or_(Contact.contact_type == "investor", Contact.fundraising_relevance_tier == "high"))
    if review_status:
        query = query.filter(Contact.review_status == review_status)
    if awaiting_reply_only:
        query = query.filter(Contact.awaiting_reply.is_(True))
    if not_replied_days is not None:
        query = query.filter(
            Contact.awaiting_reply.is_(True),
            Contact.days_since_outreach.isnot(None),
            Contact.days_since_outreach >= not_replied_days,
        )
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(
                func.lower(Contact.full_name).like(like),
                func.lower(Contact.primary_email).like(like),
                func.lower(Contact.company_name).like(like),
                func.lower(Contact.company_domain).like(like),
            )
        )
    if keyword:
        like = f"%{keyword.lower()}%"
        query = query.filter(
            or_(
                func.lower(ContactContext.auto_context_short).like(like),
                func.lower(ContactContext.auto_context_detailed).like(like),
            )
        )

    sort_column = {
        "list_number": Contact.list_number,
        "last_contacted_at": Contact.last_contacted_at,
        "first_contacted_at": Contact.first_contacted_at,
        "email_count": Contact.email_count,
        "fundraising_relevance_score": Contact.fundraising_relevance_score,
        "outreach_relevance_score": Contact.outreach_relevance_score,
        "full_name": Contact.full_name,
        "company_name": Contact.company_name,
    }.get(sort, Contact.last_contacted_at)

    if order == "asc":
        query = query.order_by(sort_column.asc().nullslast())
    else:
        query = query.order_by(sort_column.desc().nullslast())

    total = query.count()
    contacts = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [_hydrate_list_item(c) for c in contacts],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats", response_model=StatsOut)
async def contact_stats(db: Session = Depends(get_db)):
    external_filter = (Contact.is_internal.is_(False), Contact.is_excluded.is_(False))

    total_contacts = db.query(func.count(Contact.id)).scalar() or 0
    external_contacts = db.query(func.count(Contact.id)).filter(*external_filter).scalar() or 0
    high_relevance = (
        db.query(func.count(Contact.id)).filter(Contact.fundraising_relevance_tier == "high").scalar() or 0
    )
    total_messages = db.query(func.count(EmailMessage.id)).scalar() or 0
    synced_messages = (
        db.query(func.count(EmailMessage.id))
        .filter(~EmailMessage.graph_message_id.like("test-%"))
        .scalar()
        or 0
    )
    review_pending = (
        db.query(func.count(Contact.id)).filter(*external_filter, Contact.review_status == "pending").scalar() or 0
    )
    review_approved = (
        db.query(func.count(Contact.id)).filter(*external_filter, Contact.review_status == "approved").scalar() or 0
    )
    review_denied = (
        db.query(func.count(Contact.id)).filter(*external_filter, Contact.review_status == "denied").scalar() or 0
    )

    graph_sent_total: int | None = None
    sync_complete: bool | None = None
    try:
        folder = await GraphClient(db).fetch_sent_items_folder()
        graph_sent_total = folder.get("totalItemCount")
        if graph_sent_total is not None:
            sync_complete = synced_messages >= graph_sent_total
    except (GraphAuthError, httpx.HTTPError):
        pass

    last_sync = db.query(SyncRun).filter(SyncRun.status == "completed").order_by(SyncRun.completed_at.desc()).first()
    return StatsOut(
        total_contacts=total_contacts,
        external_contacts=external_contacts,
        high_relevance_contacts=high_relevance,
        total_messages=total_messages,
        synced_messages=synced_messages,
        graph_sent_total=graph_sent_total,
        sync_complete=sync_complete,
        review_pending=review_pending,
        review_approved=review_approved,
        review_denied=review_denied,
        last_sync_at=last_sync.completed_at if last_sync else None,
    )


@router.get("/{contact_id}", response_model=ContactDetail)
def get_contact(contact_id: str, db: Session = Depends(get_db)):
    contact = (
        db.query(Contact)
        .options(joinedload(Contact.context), joinedload(Contact.email_links).joinedload(ContactEmailLink.message))
        .filter(Contact.id == contact_id)
        .one_or_none()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    base = _hydrate_list_item(contact)
    context = contact.context
    return ContactDetail(
        **base.model_dump(),
        score_breakdown=contact.score_breakdown,
        auto_context_detailed=context.auto_context_detailed if context else None,
        last_meaningful_email_preview=context.last_meaningful_email_preview if context else None,
        meaningful_previews=context.meaningful_previews if context else None,
        ai_summary=context.ai_summary if context else None,
        ai_follow_up_draft=context.ai_follow_up_draft if context else None,
        ai_contact_classification=context.ai_contact_classification if context else None,
        ai_summary_generated_at=context.ai_summary_generated_at if context else None,
    )


@router.patch("/{contact_id}", response_model=ContactDetail)
def update_contact(contact_id: str, payload: ContactUpdate, db: Session = Depends(get_db)):
    contact = db.query(Contact).filter(Contact.id == contact_id).one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    updates = payload.model_dump(exclude_unset=True)
    if "review_status" in updates and updates["review_status"] not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="review_status must be pending, approved, or denied")
    for field, value in updates.items():
        setattr(contact, field, value)
    contact.updated_at = datetime.utcnow()
    db.commit()
    return get_contact(contact_id, db)


@router.get("/{contact_id}/messages")
def contact_messages(contact_id: str, db: Session = Depends(get_db)):
    messages = (
        db.query(EmailMessage)
        .join(ContactEmailLink, ContactEmailLink.email_message_id == EmailMessage.id)
        .filter(ContactEmailLink.contact_id == contact_id)
        .order_by(EmailMessage.sent_datetime.desc())
        .all()
    )
    return [
        {
            "id": m.id,
            "subject": m.subject,
            "sent_datetime": m.sent_datetime,
            "body_preview": m.body_preview,
            "outlook_weblink": m.outlook_weblink,
            "has_attachments": m.has_attachments,
            "conversation_id": m.conversation_id,
        }
        for m in messages
    ]
