from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account import MailboxAccount
from app.models.contact import Contact
from app.models.message import EmailMessage
from app.models.sync import SyncRun
from app.services.contact_pipeline import (
    process_inbound_sender,
    process_message_recipients,
    rebuild_contact_aggregates,
)
from app.services.graph_client import GraphAuthError, GraphClient
from app.services.text_utils import normalize_email, normalize_subject, parse_display_name

# Graph allows up to 999; 100 balances throughput vs payload size
GRAPH_PAGE_SIZE = 100
_YIELD_EVERY_N_MESSAGES = 50
# Small overlap so messages sent during a prior sync are not missed
_SYNC_WATERMARK_BUFFER = timedelta(minutes=1)


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
    source_account: str = "edge",
    existing_by_graph_id: dict[str, EmailMessage] | None = None,
) -> tuple[EmailMessage, bool]:
    graph_id = item["id"]
    if existing_by_graph_id is not None:
        existing = existing_by_graph_id.get(graph_id)
    else:
        existing = (
            db.query(EmailMessage)
            .filter(
                EmailMessage.graph_message_id == graph_id,
                EmailMessage.source_account == source_account,
            )
            .one_or_none()
        )
        if existing is None and source_account == "edge":
            # Pre-P2 rows may lack source_account filter match if column was null
            existing = (
                db.query(EmailMessage)
                .filter(
                    EmailMessage.graph_message_id == graph_id,
                    or_(
                        EmailMessage.source_account.is_(None),
                        EmailMessage.source_account == "",
                    ),
                )
                .one_or_none()
            )

    if existing:
        if not existing.source_account:
            existing.source_account = source_account
        return existing, False

    # graph_message_id is globally unique — if another mailbox already imported this
    # Graph id, do not insert again (same Microsoft identity connected twice).
    collision = (
        db.query(EmailMessage)
        .filter(EmailMessage.graph_message_id == graph_id)
        .one_or_none()
    )
    if collision is not None:
        return collision, False

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
        source_account=source_account,
    )
    db.add(message)
    db.flush()
    if existing_by_graph_id is not None:
        existing_by_graph_id[graph_id] = message
    return message, True


def _existing_messages_for_page(
    db: Session,
    values: list[dict],
    *,
    source_account: str,
) -> dict[str, EmailMessage]:
    """Look up existing rows for this mailbox only.

    Must be scoped by source_account — otherwise syncing mailbox B can reuse
    mailbox A's rows (same Graph identity or shared graph ids) and every Sync
    button appears to import the same mailbox.
    """
    graph_ids = [item["id"] for item in values if item.get("id")]
    if not graph_ids:
        return {}
    q = db.query(EmailMessage).filter(EmailMessage.graph_message_id.in_(graph_ids))
    if source_account == "edge":
        q = q.filter(
            or_(
                EmailMessage.source_account == "edge",
                EmailMessage.source_account.is_(None),
                EmailMessage.source_account == "",
            )
        )
    else:
        q = q.filter(EmailMessage.source_account == source_account)
    rows = q.all()
    return {m.graph_message_id: m for m in rows}


class SyncService:
    # Rebuild less often; aggregates are now batched so larger windows are fine
    BATCH_AGGREGATE_SIZE = 1000

    def __init__(self, db: Session, account_id: str = "edge"):
        self.db = db
        self.account_id = account_id
        self.graph = GraphClient(db, account_id=account_id)
        self._contact_cache: dict[str, Contact] = {}

    def assert_token_matches_mailbox(self) -> None:
        """Refuse to sync if the OAuth identity is not this mailbox's address."""
        account = self.db.get(MailboxAccount, self.account_id)
        if not account:
            raise GraphAuthError(f"Unknown mailbox account: {self.account_id}")
        token = self.graph.get_token_row()
        if not token:
            raise GraphAuthError(
                f"Not authenticated for {account.display_name}. Connect this mailbox in Settings first."
            )
        connected = normalize_email(token.user_email)
        expected = normalize_email(account.email)
        if connected and expected and connected != expected:
            raise GraphAuthError(
                f"{account.display_name} is connected as <{token.user_email}>, but this mailbox "
                f"expects <{account.email}>. Disconnect and sign in with the correct Microsoft account, "
                "then Sync again."
            )

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

    def stop_active_sync(self, reason: str = "Stopped by user") -> SyncRun | None:
        """Stop this mailbox's running sync (cooperative cancel)."""
        run = (
            self.db.query(SyncRun)
            .filter(SyncRun.status == "running", SyncRun.account_id == self.account_id)
            .order_by(SyncRun.started_at.desc())
            .first()
        )
        if not run:
            self._clear_syncing_status()
            self.db.commit()
            return None
        now = datetime.utcnow()
        run.status = "failed"
        run.error_message = reason
        run.completed_at = now
        self._clear_syncing_status()
        self.db.commit()
        self.db.refresh(run)
        return run

    def _was_cancelled(self, sync_run: SyncRun) -> bool:
        self.db.refresh(sync_run)
        return sync_run.status != "running"

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
            .filter(SyncRun.status == "running", SyncRun.account_id == self.account_id)
            .order_by(SyncRun.started_at.desc())
            .first()
        )

    def _outbound_watermark(self) -> datetime | None:
        q = self.db.query(func.max(EmailMessage.sent_datetime)).filter(
            EmailMessage.direction == "outbound",
            EmailMessage.source_account == self.account_id,
        )
        watermark = q.scalar()
        if watermark is None and self.account_id == "edge":
            watermark = (
                self.db.query(func.max(EmailMessage.sent_datetime))
                .filter(EmailMessage.direction == "outbound")
                .scalar()
            )
        return watermark

    def _inbound_watermark(self) -> datetime | None:
        q = self.db.query(func.max(EmailMessage.sent_datetime)).filter(
            EmailMessage.direction == "inbound",
            EmailMessage.source_account == self.account_id,
        )
        watermark = q.scalar()
        if watermark is None and self.account_id == "edge":
            watermark = (
                self.db.query(func.max(EmailMessage.sent_datetime))
                .filter(EmailMessage.direction == "inbound")
                .scalar()
            )
        return watermark

    def _since_filter(self, watermark: datetime | None) -> datetime | None:
        if watermark is None:
            return None
        return watermark - _SYNC_WATERMARK_BUFFER

    def _outbound_oldest(self) -> datetime | None:
        """Oldest outbound message we've imported for this mailbox."""
        q = self.db.query(func.min(EmailMessage.sent_datetime)).filter(
            EmailMessage.direction == "outbound",
            EmailMessage.source_account == self.account_id,
        )
        oldest = q.scalar()
        if oldest is None and self.account_id == "edge":
            oldest = (
                self.db.query(func.min(EmailMessage.sent_datetime))
                .filter(EmailMessage.direction == "outbound")
                .scalar()
            )
        return oldest

    def _synced_outbound_count(self) -> int:
        """Count of real (non-test) outbound messages imported for this mailbox."""
        q = self.db.query(func.count(EmailMessage.id)).filter(
            EmailMessage.direction == "outbound",
            ~EmailMessage.graph_message_id.like("test-%"),
        )
        if self.account_id == "edge":
            q = q.filter(
                or_(
                    EmailMessage.source_account == "edge",
                    EmailMessage.source_account.is_(None),
                    EmailMessage.source_account == "",
                )
            )
        else:
            q = q.filter(EmailMessage.source_account == self.account_id)
        return int(q.scalar() or 0)

    async def _needs_outbound_backfill(self) -> bool:
        """True when the Sent folder holds more messages than we've imported.

        Used after the newest-first pass to decide whether we must also walk
        backwards and import messages older than the last sync.
        """
        try:
            folder = await self.graph.fetch_sent_items_folder()
        except (GraphAuthError, httpx.HTTPError):
            return False
        total = folder.get("totalItemCount")
        if total is None:
            return False
        return self._synced_outbound_count() < int(total)

    async def _process_outbound_page(
        self,
        values: list[dict],
        *,
        messages_fetched: int,
        messages_new: int,
    ) -> tuple[int, int, list[str]]:
        existing = _existing_messages_for_page(
            self.db, values, source_account=self.account_id
        )
        batch_touched: list[str] = []
        for index, item in enumerate(values):
            message, is_new = upsert_message(
                self.db,
                item,
                source_account=self.account_id,
                existing_by_graph_id=existing,
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
        existing = _existing_messages_for_page(
            self.db, values, source_account=self.account_id
        )
        batch_touched: list[str] = []
        for index, item in enumerate(values):
            message, is_new = upsert_message(
                self.db,
                item,
                direction="inbound",
                source_account=self.account_id,
                existing_by_graph_id=existing,
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

    async def _drain_outbound_pages(
        self,
        sync_run: SyncRun,
        *,
        since: datetime | None,
        before: datetime | None,
        messages_fetched: int,
        messages_new: int,
        touched_contact_ids: set[str],
        resume_url: str | None = None,
    ) -> tuple[int, int, bool]:
        """Fetch and process Sent-items pages until exhausted or cancelled.

        Returns (messages_fetched, messages_new, completed); completed is False
        when the run was cancelled before all pages were drained.
        """
        url = resume_url
        first = url is None
        # Prefetch the next Graph page while we process the current one.
        next_page_task: asyncio.Task | None = None
        try:
            while True:
                if self._was_cancelled(sync_run):
                    return messages_fetched, messages_new, False

                if next_page_task is not None:
                    page = await next_page_task
                    next_page_task = None
                elif first:
                    page = await self.graph.fetch_messages_page(
                        None,
                        top=GRAPH_PAGE_SIZE,
                        newest_first=since is None and before is None,
                        since=since,
                        before=before,
                    )
                    first = False
                else:
                    page = await self.graph.fetch_messages_page(url, top=GRAPH_PAGE_SIZE)

                values = page.get("value", [])
                next_url = page.get("@odata.nextLink")
                if next_url:
                    next_page_task = asyncio.create_task(
                        self.graph.fetch_messages_page(next_url, top=GRAPH_PAGE_SIZE)
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

                if self._was_cancelled(sync_run):
                    if next_page_task is not None:
                        next_page_task.cancel()
                    return messages_fetched, messages_new, False

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = next_url
                self.db.commit()

                await asyncio.sleep(0)

                if not next_url:
                    break
                url = next_url
        finally:
            if next_page_task is not None and not next_page_task.done():
                next_page_task.cancel()

        return messages_fetched, messages_new, True

    async def run_full_sync(self, sync_run_id: str) -> None:
        sync_run = self.db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        touched_contact_ids: set[str] = set()
        resume_url = sync_run.checkpoint_url
        messages_new = sync_run.messages_new
        messages_fetched = sync_run.messages_fetched
        since = self._since_filter(self._outbound_watermark()) if resume_url is None else None

        try:
            # Newest-first pass: everything sent since the last sync watermark.
            messages_fetched, messages_new, completed = await self._drain_outbound_pages(
                sync_run,
                since=since,
                before=None,
                messages_fetched=messages_fetched,
                messages_new=messages_new,
                touched_contact_ids=touched_contact_ids,
                resume_url=resume_url,
            )
            if not completed:
                return

            # Backfill pass: if the Sent folder still holds more than we've
            # imported (e.g. a previous sync was stopped early), walk backwards
            # from the oldest message we have and import everything older.
            if await self._needs_outbound_backfill():
                before = self._outbound_oldest()
                if before is not None:
                    messages_fetched, messages_new, completed = await self._drain_outbound_pages(
                        sync_run,
                        since=None,
                        before=before,
                        messages_fetched=messages_fetched,
                        messages_new=messages_new,
                        touched_contact_ids=touched_contact_ids,
                    )
                    if not completed:
                        return

            if self._was_cancelled(sync_run):
                return

            if touched_contact_ids:
                updated_count = rebuild_contact_aggregates(self.db, list(touched_contact_ids))
            else:
                updated_count = 0

            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
            self._mark_account_synced()
        except Exception as exc:
            if self._was_cancelled(sync_run):
                return
            sync_run.status = "failed"
            sync_run.error_message = str(exc)
            sync_run.completed_at = datetime.utcnow()
            self._clear_syncing_status()
            self.db.commit()
            raise

    def _mark_account_synced(self) -> None:
        from app.models.account import MailboxAccount
        from app.services.connectors.graph_connector import plain_sync_label

        account = self.db.get(MailboxAccount, self.account_id)
        if not account:
            return
        now = datetime.utcnow()
        account.last_sync_at = now
        account.last_sync_plain = plain_sync_label(now)
        account.status = "connected"
        account.updated_at = now
        self.db.commit()

    def _clear_syncing_status(self) -> None:
        account = self.db.get(MailboxAccount, self.account_id)
        if account and account.status == "syncing":
            account.status = "connected"
            account.updated_at = datetime.utcnow()

    async def run_inbox_sync(self, sync_run_id: str) -> None:
        sync_run = self.db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        touched_contact_ids: set[str] = set()
        url = sync_run.checkpoint_url
        messages_new = sync_run.messages_new
        messages_fetched = sync_run.messages_fetched
        since = self._since_filter(self._inbound_watermark()) if url is None else None
        next_page_task: asyncio.Task | None = None

        try:
            while True:
                if self._was_cancelled(sync_run):
                    return

                if next_page_task is not None:
                    page = await next_page_task
                    next_page_task = None
                else:
                    page = await self.graph.fetch_inbox_page(url, since=since)

                values = page.get("value", [])
                next_url = page.get("@odata.nextLink")
                if next_url:
                    next_page_task = asyncio.create_task(
                        self.graph.fetch_inbox_page(next_url, since=since)
                    )

                messages_fetched, messages_new, batch_touched = await self._process_inbound_page(
                    values,
                    messages_fetched=messages_fetched,
                    messages_new=messages_new,
                )
                touched_contact_ids.update(batch_touched)

                if len(touched_contact_ids) >= self.BATCH_AGGREGATE_SIZE:
                    rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                    touched_contact_ids.clear()

                if self._was_cancelled(sync_run):
                    if next_page_task is not None:
                        next_page_task.cancel()
                    return

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = next_url
                self.db.commit()

                await asyncio.sleep(0)

                if not next_url:
                    break
                url = next_url

            if self._was_cancelled(sync_run):
                return

            if touched_contact_ids:
                updated_count = rebuild_contact_aggregates(self.db, list(touched_contact_ids))
            else:
                updated_count = 0

            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
            self._mark_account_synced()
            self._refresh_campaign_tracking()
        except Exception as exc:
            if next_page_task is not None:
                next_page_task.cancel()
            if self._was_cancelled(sync_run):
                return
            sync_run.status = "failed"
            sync_run.error_message = str(exc)
            sync_run.completed_at = datetime.utcnow()
            self._clear_syncing_status()
            self.db.commit()
            raise

    def _refresh_campaign_tracking(self) -> None:
        import logging

        from app.models.campaign import Campaign
        from app.services.campaign_tracking import refresh_campaign_tracking

        logger = logging.getLogger(__name__)
        if not get_settings().feature_compass_tracking:
            return
        campaigns = (
            self.db.query(Campaign)
            .filter(Campaign.status.in_(("tracking", "scheduled", "completed", "ready_to_save")))
            .limit(20)
            .all()
        )
        for c in campaigns:
            try:
                refresh_campaign_tracking(self.db, c.id)
            except Exception:
                logger.exception("Campaign tracking refresh failed for %s", c.id)

    def start_inbox_sync(self) -> SyncRun:
        active = self.get_active_run()
        if active:
            return active
        sync_run = SyncRun(sync_type="inbox", status="running", account_id=self.account_id)
        self.db.add(sync_run)
        self.db.commit()
        self.db.refresh(sync_run)
        return sync_run

    def start_full_sync(self) -> SyncRun:
        active = self.get_active_run()
        if active:
            return active

        sync_type = "incremental" if self._outbound_watermark() else "full"
        sync_run = SyncRun(sync_type=sync_type, status="running", account_id=self.account_id)
        self.db.add(sync_run)
        self.db.commit()
        self.db.refresh(sync_run)
        return sync_run

    async def run_gmail_sync(self, sync_run_id: str, *, folder: str = "sent") -> None:
        """Import Gmail Sent (or Inbox) into EmailMessage with source_account=northwyn."""
        from app.services.gmail_client import GmailAuthError, GmailClient, gmail_message_to_graph_shape

        sync_run = self.db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        gmail = GmailClient(self.db, account_id=self.account_id)
        try:
            gmail.ensure_access_token()
        except GmailAuthError:
            sync_run.status = "failed"
            sync_run.error_message = "Gmail not authenticated"
            sync_run.completed_at = datetime.utcnow()
            self._clear_syncing_status()
            self.db.commit()
            raise

        query = "in:inbox" if folder == "inbox" else "in:sent"
        direction = "inbound" if folder == "inbox" else "outbound"
        page_token: str | None = None
        messages_fetched = sync_run.messages_fetched or 0
        messages_new = sync_run.messages_new or 0
        touched_contact_ids: set[str] = set()
        max_pages = 50  # safety cap (~5k messages at 100/page)

        try:
            for _ in range(max_pages):
                if self._was_cancelled(sync_run):
                    return

                listing = await gmail.list_message_refs(
                    query=query, page_token=page_token, max_results=50
                )
                refs = listing.get("messages") or []
                if not refs:
                    break

                values: list[dict] = []
                for ref in refs:
                    raw = await gmail.fetch_message(ref["id"], format="full")
                    values.append(gmail_message_to_graph_shape(raw, direction=direction))

                if direction == "inbound":
                    messages_fetched, messages_new, batch = await self._process_inbound_page(
                        values, messages_fetched=messages_fetched, messages_new=messages_new
                    )
                else:
                    messages_fetched, messages_new, batch = await self._process_outbound_page(
                        values, messages_fetched=messages_fetched, messages_new=messages_new
                    )
                touched_contact_ids.update(batch)

                sync_run.messages_fetched = messages_fetched
                sync_run.messages_new = messages_new
                sync_run.checkpoint_url = listing.get("nextPageToken")
                self.db.commit()

                page_token = listing.get("nextPageToken")
                if not page_token:
                    break
                await asyncio.sleep(0)

            if self._was_cancelled(sync_run):
                return

            updated_count = (
                rebuild_contact_aggregates(self.db, list(touched_contact_ids))
                if touched_contact_ids
                else 0
            )
            sync_run.status = "completed"
            sync_run.completed_at = datetime.utcnow()
            sync_run.contacts_updated = updated_count
            self.db.commit()
            self._mark_account_synced()
            if folder == "inbox":
                self._refresh_campaign_tracking()
        except Exception as exc:
            if self._was_cancelled(sync_run):
                return
            sync_run.status = "failed"
            sync_run.error_message = str(exc)
            sync_run.completed_at = datetime.utcnow()
            self._clear_syncing_status()
            self.db.commit()
            raise


async def run_sync_in_background(db_factory, sync_run_id: str) -> None:
    db = db_factory()
    try:
        sync_run = db.query(SyncRun).filter(SyncRun.id == sync_run_id).one()
        account_id = sync_run.account_id or "edge"
        service = SyncService(db, account_id=account_id)
        # Northwyn / Gmail path
        if account_id == "northwyn":
            folder = "inbox" if sync_run.sync_type == "inbox" else "sent"
            await service.run_gmail_sync(sync_run_id, folder=folder)
        elif sync_run.sync_type == "inbox":
            await service.run_inbox_sync(sync_run_id)
        else:
            await service.run_full_sync(sync_run_id)
    finally:
        db.close()
