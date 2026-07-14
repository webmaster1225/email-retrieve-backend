#!/usr/bin/env python3
"""Re-extract contacts from all imported messages (run after parsing fix)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app.models.contact import Contact, ContactEmailLink
from app.models.message import EmailMessage
from app.services.contact_pipeline import process_message_recipients, rebuild_contact_aggregates
from sqlalchemy import func


def main() -> None:
    db = SessionLocal()
    try:
        from app.models.contact import ContactContext
        from app.models.message import ConversationThread

        print("Clearing existing contacts (keeping messages)...")
        db.query(ContactEmailLink).delete()
        db.query(ConversationThread).delete()
        db.query(ContactContext).delete()
        db.query(Contact).delete()
        db.commit()

        messages = db.query(EmailMessage).order_by(EmailMessage.sent_datetime.asc()).all()
        print(f"Reprocessing {len(messages)} messages...")
        touched: set[str] = set()
        batch = 0
        for i, message in enumerate(messages, 1):
            ids = process_message_recipients(db, message)
            touched.update(ids)
            if i % 500 == 0:
                db.commit()
                rebuild_contact_aggregates(db, list(touched))
                touched.clear()
                print(f"  {i}/{len(messages)} messages processed...")
        db.commit()
        print(f"Rebuilding aggregates for all contacts...")
        rebuild_contact_aggregates(db, None)

        total = db.query(func.count(Contact.id)).scalar()
        external = (
            db.query(func.count(Contact.id))
            .filter(Contact.is_internal.is_(False), Contact.is_excluded.is_(False))
            .scalar()
        )
        links = db.query(func.count(ContactEmailLink.id)).scalar()
        print(f"Done. Contacts: {total}, external: {external}, links: {links}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
