from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from sqlalchemy.orm import Session, joinedload

from app.models.contact import Contact, ContactContext, ContactEmailLink


def _contact_row(contact: Contact) -> dict:
    context: ContactContext | None = contact.context
    latest_message = None
    if contact.email_links:
        latest_message = max(contact.email_links, key=lambda link: link.message.sent_datetime).message
    return {
        "#": contact.list_number,
        "Review Status": contact.review_status or "pending",
        "Name": contact.full_name,
        "Email": contact.primary_email,
        "Company": contact.company_name,
        "Domain": contact.company_domain,
        "Phone": contact.phone_number,
        "First Contacted": contact.first_contacted_at.isoformat() if contact.first_contacted_at else "",
        "Last Contacted": contact.last_contacted_at.isoformat() if contact.last_contacted_at else "",
        "Email Count": contact.email_count,
        "Thread Count": contact.thread_count,
        "Last Subject": latest_message.subject if latest_message else "",
        "Last Body Preview": context.last_meaningful_email_preview if context else (latest_message.body_preview if latest_message else ""),
        "Outlook Link": latest_message.outlook_weblink if latest_message else "",
        "Detected Topics": ", ".join(context.detected_topics or []) if context else "",
        "Fundraising Score": contact.fundraising_relevance_score,
        "Fundraising Tier": contact.fundraising_relevance_tier,
        "Contact Type": contact.contact_type,
        "Notes": contact.notes or "",
        "Tags": "",
        "Context": context.auto_context_short if context else "",
    }


def query_contacts_for_export(db: Session, contact_ids: list[str] | None = None) -> list[Contact]:
    query = (
        db.query(Contact)
        .options(
            joinedload(Contact.context),
            joinedload(Contact.email_links).joinedload(ContactEmailLink.message),
        )
        .filter(Contact.is_excluded.is_(False))
        .order_by(Contact.fundraising_relevance_score.desc(), Contact.last_contacted_at.desc())
    )
    if contact_ids:
        query = query.filter(Contact.id.in_(contact_ids))
    return query.all()


def export_contacts_xlsx(db: Session, contact_ids: list[str] | None = None) -> bytes:
    contacts = query_contacts_for_export(db, contact_ids)
    rows = [_contact_row(c) for c in contacts]
    headers = list(rows[0].keys()) if rows else [
        "Name", "Email", "Company", "Domain", "Phone", "First Contacted", "Last Contacted",
        "Email Count", "Thread Count", "Last Subject", "Last Body Preview", "Outlook Link",
        "Detected Topics", "Fundraising Score", "Fundraising Tier", "Contact Type", "Notes", "Tags", "Context",
    ]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Contacts"
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(h, "") for h in headers])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def export_contacts_csv(db: Session, contact_ids: list[str] | None = None) -> str:
    import csv

    contacts = query_contacts_for_export(db, contact_ids)
    rows = [_contact_row(c) for c in contacts]
    headers = list(rows[0].keys()) if rows else ["Name", "Email", "Company"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
