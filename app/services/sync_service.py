from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.contact import Contact
from app.models.message import EmailMessage
from app.models.sync import SyncRun
from app.services.contact_pipeline import (
    process_inbound_sender,
    process_message_recipients,
    rebuild_contact_aggregates,
)
from app.services.graph_client import GraphClient
from app.services.text_utils import normalize_email, normalize_subject, parse_display_name

# Graph allows up to 999; 100 balances throughput vs payload size
GRAPH_PAGE_SIZE = 200
_YIELD_EVERY_N_MESSAGES = 50


def _serialize_recipients(recipients: list[dict] | None) -> list[dict]:
    serialized: list[dict] = []
    for recipient in recipients or []:
        name, email = parse_display_name(recipient)
        if email:
            serialized.append({"name": name, "address": email})
    return serialized


def upsert_message(
    db: Session,
    item: dict,
    *,
    direction: str = "outbound",
    existing_by_graph_id: dict[str, EmailMessage] | None = None,
) -> tuple[EmailMessage, bool]:
    graph_id = item["id"]
    if existing_by_graph_id is not None:
        existing = existing_by_graph_id.get(graph_id)
    else:
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
    if existing_by_graph_id is not None:
        existing_by_graph_id[graph_id] = message
    return message, True


def _existing_messages_for_page(db: Session, values: list[dict]) -> dict[str, EmailMessage]:
    graph_ids = [item["id"] for item in values if item.get("id")]
    if not graph_ids:
        return {}
    rows = db.query(EmailMessage).filter(EmailMessage.graph_message_id.in_(graph_ids)).all()
    return {m.graph_message_id: m for m in rows}


class SyncService:
    # Rebuild less often; aggregates are now batched so larger windows are fine
    BATCH_AGGREGATE_SIZE = 1000

    def __init__(self, db: Session):
        self.db = db
        self.graph = GraphClient(db)
        self._contact_cache: dict[str, Contact] = {}

    def fail_running_syncs(self, reason: str = "Manually cleared stuck sync") -> list[SyncRun]:
        """Mark all running syncs as failed so a new sync can start."""
        now = datetime.utcnow()
        runs = self.db.query(SyncRun).filter(SyncRun.status == "running").all()
        for run in runs:
            run.status = "failed"
            run.error_message = reason
            run.completed_at = now
        if runs:
            self.db.commit()
            for run in runs:
                self.db.refresh(run)
        return runs

    def _fail_stale_running(self) -> None:
        hours = get_settings().sync_stale_hours
        if hours <= 0:
            return
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        stale = (
            self.db.query(SyncRun)
            .filter(SyncRun.status == "running", SyncRun.started_at < cutoff)
            .all()
        )
        if not stale:
            return
        now = datetime.utcnow()
        for run in stale:
            run.status = "failed"
            run.error_message = f"Marked failed: still running after {hours:g} hours"
            run.completed_at = now
        self.db.commit()

    def get_active_run(self) -> SyncRun | None:
        self._fail_stale_running()
        return (
            self.db.query(SyncRun)
            .filter(SyncRun.status == "running")
            .order_by(SyncRun.started_at.desc())
            .first()
        )

    async def _process_outbound_page(
        self,
        values: list[dict],
        *,
        messages_fetched: int,
        messages_new: int,
    ) -> tuple[int, int, list[str]]:
        existing = _existing_messages_for_page(self.db, values)
        batch_touched: list[str] = []
        for index, item in enumerate(values):
            message, is_new = upsert_message(
                self.db, item, existing_by_graph_id=existing
            )
            messages_fetched += 1
            if is_new:
                messages_new += 1
                contact_ids = process_message_recipients(
                    self.db, message, contact_cache=self._contact_cache
                )
                batch_touched.extend(contact_ids)
            if index > 0 and index % _YIELD_EVERY_N_MESSAGES == 0:
                await asyncio.sleep(0)
        return messages_fetched, messages_new, batch_touched

    async def _process_inbound_page(
        self,
        values: list[dict],
        *,
        messages_fetched: int,
        messages_new: int,
    ) -> tuple[int, int, list[str]]:
        existing = _existing_messages_for_page(self.db, values)
        batch_touched: list[str] = []
        for index, item in enumerate(values):
            message, is_new = upsert_message(
                self.db, item, direction="inbound", existing_by_graph_id=existing
            )
            messages_fetched += 1
            if is_new:
                messages_new += 1
                contact_ids = process_inbound_sender(
                    self.db, message, contact_cache=self._contact_cache
                )
                batch_touched.extend(contact_ids)
            if index > 0 and index % _YIELD_EVERY_N_MESSAGES == 0:
                await asyncio.sleep(0)
        return messages_fetched, messages_new, batch_touched

    async def run_full_sync(self, sync_run_id: str) -> None:
        sync_run = self.db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        touched_contact_ids: set[str] = set()
        url = sync_run.checkpoint_url
        messages_new = sync_run.messages_new
        messages_fetched = sync_run.messages_fetched
        # Prefetch the next Graph page while we process the current one
        next_page_task: asyncio.Task | None = None

        try:
            while True:
                if next_page_task is not None:
                    page = await next_page_task
                    next_page_task = None
                else:
                    page = await self.graph.fetch_messages_page(
                        url, top=GRAPH_PAGE_SIZE, newest_first=False
                    )

                values = page.get("value", [])
                next_url = page.get("@odata.nextLink")
                if next_url:
                    next_page_task = asyncio.create_task(
                        self.graph.fetch_messages_page(next_url, top=GRAPH_PAGE_SIZE, newest_first=False)
                    )

                messages_fetched, messages_new, batch_touched = await self._process_outbound_page(
                    values,
                    messages_fetched=messages_fetched,
                    messages_new=messages_new,
                )
                touched_contact_ids.update(batch_touched)

                if len(touched_contact_ids) >= self.BATCH_AGGREGATE_SIZE:
                    rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                    touched_contact_ids.clear()

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = next_url
                self.db.commit()

                await asyncio.sleep(0)

                if not next_url:
                    break
                url = next_url

            if touched_contact_ids:
                updated_count = rebuild_contact_aggregates(self.db, list(touched_contact_ids))
            else:
                updated_count = 0

            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
        except Exception as exc:
            if next_page_task is not None:
                next_page_task.cancel()
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
        next_page_task: asyncio.Task | None = None

        try:
            while True:
                if next_page_task is not None:
                    page = await next_page_task
                    next_page_task = None
                else:
                    page = await self.graph.fetch_inbox_page(url)

                values = page.get("value", [])
                next_url = page.get("@odata.nextLink")
                if next_url:
                    next_page_task = asyncio.create_task(self.graph.fetch_inbox_page(next_url))

                messages_fetched, messages_new, batch_touched = await self._process_inbound_page(
                    values,
                    messages_fetched=messages_fetched,
                    messages_new=messages_new,
                )
                touched_contact_ids.update(batch_touched)

                if len(touched_contact_ids) >= self.BATCH_AGGREGATE_SIZE:
                    rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                    touched_contact_ids.clear()

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = next_url
                self.db.commit()

                await asyncio.sleep(0)

                if not next_url:
                    break
                url = next_url

            if touched_contact_ids:
                updated_count = rebuild_contact_aggregates(self.db, list(touched_contact_ids))
            else:
                updated_count = 0

            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
        except Exception as exc:
            if next_page_task is not None:
                next_page_task.cancel()
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
