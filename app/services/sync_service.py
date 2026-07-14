from __future__ import annotations

import asyncio
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.message import EmailMessage
from app.models.sync import SyncRun
from app.services.contact_pipeline import (
    process_inbound_sender,
    process_message_recipients,
    rebuild_contact_aggregates,
)
from app.services.graph_client import GraphClient
from app.services.text_utils import normalize_email, normalize_subject, parse_display_name


def _serialize_recipients(recipients: list[dict] | None) -> list[dict]:
    serialized: list[dict] = []
    for recipient in recipients or []:
        name, email = parse_display_name(recipient)
        if email:
            serialized.append({"name": name, "address": email})
    return serialized


def upsert_message(db: Session, item: dict, *, direction: str = "outbound") -> tuple[EmailMessage, bool]:
    graph_id = item["id"]
    existing = db.query(EmailMessage).filter(EmailMessage.graph_message_id == graph_id).one_or_none()
    if existing:
        return existing, False

    sender = item.get("sender") or item.get("from") or {}
    _, sender_email = parse_display_name(sender)

    if direction == "inbound":
        dt_raw = item.get("receivedDateTime") or item.get("sentDateTime")
    else:
        dt_raw = item["sentDateTime"]
    sent_dt = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))

    message = EmailMessage(
        graph_message_id=graph_id,
        internet_message_id=item.get("internetMessageId"),
        conversation_id=item.get("conversationId"),
        sent_datetime=sent_dt,
        subject=item.get("subject"),
        subject_normalized=normalize_subject(item.get("subject")),
        body_preview=item.get("bodyPreview"),
        outlook_weblink=item.get("webLink"),
        has_attachments=bool(item.get("hasAttachments")),
        importance=item.get("importance"),
        categories=item.get("categories") or [],
        sender_email=normalize_email(sender_email),
        raw_from=sender,
        raw_to=_serialize_recipients(item.get("toRecipients")),
        raw_cc=_serialize_recipients(item.get("ccRecipients")),
        raw_bcc=_serialize_recipients(item.get("bccRecipients")),
        direction=direction,
    )
    db.add(message)
    db.flush()
    return message, True


class SyncService:
    BATCH_AGGREGATE_SIZE = 250

    def __init__(self, db: Session):
        self.db = db
        self.graph = GraphClient(db)

    def get_active_run(self) -> SyncRun | None:
        return (
            self.db.query(SyncRun)
            .filter(SyncRun.status == "running")
            .order_by(SyncRun.started_at.desc())
            .first()
        )

    async def run_full_sync(self, sync_run_id: str) -> None:
        sync_run = self.db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        touched_contact_ids: set[str] = set()
        url = sync_run.checkpoint_url
        messages_new = sync_run.messages_new
        messages_fetched = sync_run.messages_fetched

        try:
            while True:
                page = await self.graph.fetch_messages_page(url)
                values = page.get("value", [])
                batch_touched: list[str] = []

                for item in values:
                    message, is_new = upsert_message(self.db, item)
                    messages_fetched += 1
                    if is_new:
                        messages_new += 1
                        contact_ids = process_message_recipients(self.db, message)
                        batch_touched.extend(contact_ids)

                self.db.commit()
                touched_contact_ids.update(batch_touched)

                if len(touched_contact_ids) >= self.BATCH_AGGREGATE_SIZE:
                    rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                    touched_contact_ids.clear()

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = page.get("@odata.nextLink")
                self.db.commit()

                url = page.get("@odata.nextLink")
                if not url:
                    break

            if touched_contact_ids:
                updated_count = rebuild_contact_aggregates(self.db, list(touched_contact_ids))
            else:
                updated_count = 0

            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
        except Exception as exc:
            sync_run.status = "failed"
            sync_run.error_message = str(exc)
            sync_run.completed_at = datetime.utcnow()
            self.db.commit()
            raise

    async def run_inbox_sync(self, sync_run_id: str) -> None:
        sync_run = self.db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        touched_contact_ids: set[str] = set()
        url = sync_run.checkpoint_url
        messages_new = sync_run.messages_new
        messages_fetched = sync_run.messages_fetched

        try:
            while True:
                page = await self.graph.fetch_inbox_page(url)
                values = page.get("value", [])
                batch_touched: list[str] = []

                for item in values:
                    message, is_new = upsert_message(self.db, item, direction="inbound")
                    messages_fetched += 1
                    if is_new:
                        messages_new += 1
                        contact_ids = process_inbound_sender(self.db, message)
                        batch_touched.extend(contact_ids)

                self.db.commit()
                touched_contact_ids.update(batch_touched)

                if len(touched_contact_ids) >= self.BATCH_AGGREGATE_SIZE:
                    rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                    touched_contact_ids.clear()

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = page.get("@odata.nextLink")
                self.db.commit()

                url = page.get("@odata.nextLink")
                if not url:
                    break

            if touched_contact_ids:
                updated_count = rebuild_contact_aggregates(self.db, list(touched_contact_ids))
            else:
                updated_count = 0

            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
        except Exception as exc:
            sync_run.status = "failed"
            sync_run.error_message = str(exc)
            sync_run.completed_at = datetime.utcnow()
            self.db.commit()
            raise

    def start_inbox_sync(self) -> SyncRun:
        active = self.get_active_run()
        if active:
            return active
        sync_run = SyncRun(sync_type="inbox", status="running")
        self.db.add(sync_run)
        self.db.commit()
        self.db.refresh(sync_run)
        return sync_run

    def start_full_sync(self) -> SyncRun:
        active = self.get_active_run()
        if active:
            return active

        sync_run = SyncRun(sync_type="full", status="running")
        self.db.add(sync_run)
        self.db.commit()
        self.db.refresh(sync_run)
        return sync_run


async def run_sync_in_background(db_factory, sync_run_id: str) -> None:
    db = db_factory()
    try:
        service = SyncService(db)
        sync_run = db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        if sync_run.sync_type == "inbox":
            await service.run_inbox_sync(sync_run_id)
        else:
            await service.run_full_sync(sync_run_id)
    finally:
        db.close()
