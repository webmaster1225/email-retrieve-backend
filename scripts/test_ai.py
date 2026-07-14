#!/usr/bin/env python3
"""Seed a sample contact and test AI + API endpoints."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal, init_db
from app.models.contact import Contact, ContactContext, ContactEmailLink
from app.models.message import EmailMessage
from app.services.ai_service import generate_summary, classify_contact, generate_follow_up, strip_html
from app.config import get_settings


def seed_sample_contact(db) -> str:
    """Dev-only fake contact for AI testing. Not real Outlook data."""
    existing = db.query(Contact).filter(Contact.primary_email == "john.smith@abccapital.com").first()
    if existing:
        return existing.id

    contact = Contact(
        full_name="John Smith",
        primary_email="john.smith@abccapital.com",
        company_name="ABC Capital",
        company_domain="abccapital.com",
        first_contacted_at=datetime.utcnow() - timedelta(days=400),
        last_contacted_at=datetime.utcnow() - timedelta(days=14),
        email_count=3,
        thread_count=2,
        fundraising_relevance_score=65,
        fundraising_relevance_tier="high",
        contact_type="investor",
    )
    db.add(contact)
    db.flush()

    messages = []
    for i, (subject, preview) in enumerate(
        [
            ("Intro: Galaxy Pharma investor deck", "Hi John — sharing our Galaxy Pharma deck for your review ahead of a potential family office intro."),
            ("Re: Galaxy Pharma Deck", "Thanks for the overview. Happy to discuss the 503B formulation strategy and capital raise timeline next week."),
            ("Re: Follow-up call", "Yes, let's reconnect next week. Thanks & Regards."),
        ]
    ):
        msg = EmailMessage(
            graph_message_id=f"test-graph-id-{i}",
            conversation_id=f"conv-{i // 2}",
            sent_datetime=datetime.utcnow() - timedelta(days=100 - i * 30),
            subject=subject,
            body_preview=preview,
            outlook_weblink="https://outlook.office.com/test",
            has_attachments=i == 0,
            sender_email="dbains@edgeinvesting.ca",
            raw_to=[{"name": "John Smith", "address": "john.smith@abccapital.com"}],
            raw_cc=[],
            raw_bcc=[],
        )
        db.add(msg)
        db.flush()
        messages.append(msg)
        db.add(ContactEmailLink(contact_id=contact.id, email_message_id=msg.id, recipient_type="to"))

    ctx = ContactContext(
        contact_id=contact.id,
        auto_context_short="Contacted 3 times. Topics: fundraising, pharma.",
        auto_context_detailed="Investor relationship regarding Galaxy Pharma deck and capital raise.",
        detected_topics=["fundraising", "pharma", "meeting"],
        meaningful_previews=[m.body_preview for m in messages[:2]],
    )
    db.add(ctx)
    db.commit()
    return contact.id


async def test_ai(contact_id: str) -> None:
    db = SessionLocal()
    try:
        print("Testing AI summary...")
        result = await generate_summary(db, contact_id, force=True)
        assert result["summary"]
        print(f"  Summary OK ({len(result['summary'])} chars, cached={result['cached']})")

        print("Testing AI classify...")
        result = await classify_contact(db, contact_id, force=True)
        assert result["classification"]["contact_type"]
        print(f"  Classify OK: {result['classification']}")

        print("Testing AI follow-up...")
        result = await generate_follow_up(db, contact_id, force=True)
        assert result["draft"]
        print(f"  Follow-up OK ({len(result['draft'])} chars)")
    finally:
        db.close()


def test_strip_html() -> None:
    assert "Hello" in strip_html("<p>Hello <b>world</b></p>")
    print("HTML strip OK")


def test_config() -> None:
    settings = get_settings()
    assert settings.anthropic_api_key.startswith("sk-ant-")
    assert settings.azure_client_id
    print("Config OK")


async def main() -> None:
    init_db()
    test_config()
    test_strip_html()
    db = SessionLocal()
    try:
        contact_id = seed_sample_contact(db)
        print(f"Sample contact id: {contact_id}")
    finally:
        db.close()

    await test_ai(contact_id)
    print("\nAll tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
