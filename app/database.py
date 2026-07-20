from __future__ import annotations

import logging
import os
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

settings = get_settings()
data_dir = BASE_DIR / "data"
data_dir.mkdir(parents=True, exist_ok=True)

_IS_SQLITE = settings.database_url.startswith("sqlite")
_ON_AZURE = bool(os.getenv("WEBSITE_SITE_NAME"))

if _IS_SQLITE:
    # Ensure the SQLite file's parent directory exists (e.g. /home/data on Azure)
    db_path = settings.database_url.split("sqlite:///", 1)[-1]
    if db_path and db_path not in (":memory:",):
        Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if _IS_SQLITE else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # Wait instead of failing immediately when another connection holds a lock
        cursor.execute("PRAGMA busy_timeout=30000")
        # WAL is great locally; avoid it on Azure Files (/home) where SMB locking is unreliable
        if not _ON_AZURE:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def cleanup_orphaned_jobs() -> int:
    """Mark in-flight sync/outreach rows as failed after a process restart.

    BackgroundTasks die with the worker; without this, status stays 'running'
    forever and blocks every future sync start.
    """
    from app.models.outreach import OutreachJob
    from app.models.sync import SyncRun

    db = SessionLocal()
    cleared = 0
    try:
        now = datetime.utcnow()
        message = "Interrupted by process restart"
        for run in db.query(SyncRun).filter(SyncRun.status == "running").all():
            run.status = "failed"
            run.error_message = message
            run.completed_at = now
            cleared += 1
        for job in db.query(OutreachJob).filter(OutreachJob.status == "running").all():
            job.status = "failed"
            job.error_message = message
            job.completed_at = now
            job.updated_at = now
            cleared += 1
        if cleared:
            db.commit()
            logger.warning("Cleared %s orphaned running job(s) after startup", cleared)
        return cleared
    finally:
        db.close()


def _assign_contact_list_numbers(conn) -> None:
    from sqlalchemy import text

    rows = conn.execute(
        text(
            """
            SELECT id FROM contacts
            WHERE is_internal = 0 AND is_excluded = 0
            ORDER BY fundraising_relevance_score DESC,
                     CASE WHEN last_contacted_at IS NULL THEN 1 ELSE 0 END,
                     last_contacted_at DESC,
                     primary_email ASC
            """
        )
    ).fetchall()
    for index, (contact_id,) in enumerate(rows, start=1):
        conn.execute(
            text("UPDATE contacts SET list_number = :num WHERE id = :id"),
            {"num": index, "id": contact_id},
        )


def seed_mailbox_accounts() -> None:
    """Ensure the four canonical mailbox rows exist and mirror feature flags."""
    from app.config import get_settings
    from app.models.account import ACCOUNT_SEEDS, MailboxAccount
    from app.models.sync import AuthToken

    settings = get_settings()
    db = SessionLocal()
    try:
        # Clear any leftover stub tokens from P2 stubs-first mode
        stub_tokens = (
            db.query(AuthToken)
            .filter(AuthToken.access_token.like("stub-%"))
            .all()
        )
        for tok in stub_tokens:
            acct = db.get(MailboxAccount, tok.account_id) if tok.account_id else None
            if acct and acct.status in ("connected", "syncing"):
                acct.status = "not_connected"
                acct.permissions_json = {}
                acct.is_stub = False
            db.delete(tok)

        for seed in ACCOUNT_SEEDS:
            row = db.get(MailboxAccount, seed["id"])
            enabled = settings.account_feature_enabled(seed["id"])
            is_stub = False  # Real OAuth only

            if row is None:
                row = MailboxAccount(
                    id=seed["id"],
                    display_name=seed["display_name"],
                    email=seed["email"],
                    provider=seed["provider"],
                    blurb=seed["blurb"],
                    is_functional=seed["is_functional"],
                    default_included=seed["default_included"],
                    enabled=enabled,
                    is_stub=is_stub,
                    status="not_connected",
                    permissions_json={},
                )
                db.add(row)
            else:
                row.display_name = seed["display_name"]
                row.email = seed["email"]
                row.provider = seed["provider"]
                row.blurb = seed["blurb"]
                row.is_functional = seed["is_functional"]
                row.default_included = seed["default_included"]
                row.enabled = enabled
                row.is_stub = False
                row.updated_at = datetime.utcnow()

        # Migrate legacy AuthToken rows (no account_id) → edge
        legacy = (
            db.query(AuthToken)
            .filter((AuthToken.account_id.is_(None)) | (AuthToken.account_id == ""))
            .order_by(AuthToken.updated_at.desc())
            .all()
        )
        if legacy:
            edge = db.get(MailboxAccount, "edge")
            keep = legacy[0]
            keep.account_id = "edge"
            keep.updated_at = datetime.utcnow()
            for extra in legacy[1:]:
                db.delete(extra)
            if edge and edge.status == "not_connected":
                edge.status = "connected"
                edge.permissions_json = {
                    "read_mail": True,
                    "send": True,
                    "calendar": False,
                    "drafts": True,
                }
                edge.updated_at = datetime.utcnow()

        # Also attach any edge token without status update
        edge_token = (
            db.query(AuthToken)
            .filter(AuthToken.account_id == "edge")
            .order_by(AuthToken.updated_at.desc())
            .first()
        )
        edge = db.get(MailboxAccount, "edge")
        if edge_token and edge and edge.status == "not_connected":
            edge.status = "connected"
            edge.permissions_json = {
                "read_mail": True,
                "send": True,
                "calendar": False,
                "drafts": True,
            }

        db.commit()
    finally:
        db.close()


def run_migrations() -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "contacts" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("contacts")}
    with engine.begin() as conn:
        if "review_status" not in columns:
            conn.execute(
                text("ALTER TABLE contacts ADD COLUMN review_status VARCHAR(16) DEFAULT 'pending'")
            )
        if "list_number" not in columns:
            conn.execute(text("ALTER TABLE contacts ADD COLUMN list_number INTEGER"))

        needs_numbers = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM contacts
                WHERE list_number IS NULL AND is_internal = 0 AND is_excluded = 0
                """
            )
        ).scalar()
        if needs_numbers:
            _assign_contact_list_numbers(conn)

    if "email_messages" in inspector.get_table_names():
        msg_columns = {column["name"] for column in inspector.get_columns("email_messages")}
        with engine.begin() as conn:
            if "direction" not in msg_columns:
                conn.execute(
                    text("ALTER TABLE email_messages ADD COLUMN direction VARCHAR(16) DEFAULT 'outbound'")
                )
            if "source_account" not in msg_columns:
                conn.execute(
                    text(
                        "ALTER TABLE email_messages ADD COLUMN source_account VARCHAR(32) DEFAULT 'edge'"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_email_messages_source_account "
                        "ON email_messages (source_account)"
                    )
                )

    if "auth_tokens" in inspector.get_table_names():
        tok_columns = {column["name"] for column in inspector.get_columns("auth_tokens")}
        with engine.begin() as conn:
            if "account_id" not in tok_columns:
                conn.execute(text("ALTER TABLE auth_tokens ADD COLUMN account_id VARCHAR(32)"))
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_auth_tokens_account_id "
                        "ON auth_tokens (account_id)"
                    )
                )

    if "sync_runs" in inspector.get_table_names():
        sync_columns = {column["name"] for column in inspector.get_columns("sync_runs")}
        with engine.begin() as conn:
            if "account_id" not in sync_columns:
                conn.execute(
                    text("ALTER TABLE sync_runs ADD COLUMN account_id VARCHAR(32) DEFAULT 'edge'")
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_sync_runs_account_id "
                        "ON sync_runs (account_id)"
                    )
                )

    if "contact_context" in inspector.get_table_names():
        ctx_columns = {column["name"] for column in inspector.get_columns("contact_context")}
        with engine.begin() as conn:
            if "ai_seniority" not in ctx_columns:
                conn.execute(text("ALTER TABLE contact_context ADD COLUMN ai_seniority JSON"))
            if "ai_relationship_analysis" not in ctx_columns:
                conn.execute(text("ALTER TABLE contact_context ADD COLUMN ai_relationship_analysis JSON"))
            if "ai_outreach_intelligence" not in ctx_columns:
                conn.execute(text("ALTER TABLE contact_context ADD COLUMN ai_outreach_intelligence JSON"))

    if "contacts" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("contacts")}
        with engine.begin() as conn:
            if "last_inbound_at" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN last_inbound_at DATETIME"))
            if "last_outbound_at" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN last_outbound_at DATETIME"))
            if "awaiting_reply" not in columns:
                conn.execute(
                    text("ALTER TABLE contacts ADD COLUMN awaiting_reply BOOLEAN DEFAULT 0")
                )
            if "days_since_outreach" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN days_since_outreach INTEGER"))
            if "outreach_relevance_score" not in columns:
                conn.execute(
                    text("ALTER TABLE contacts ADD COLUMN outreach_relevance_score INTEGER DEFAULT 0")
                )
            if "outreach_relevance_tier" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN outreach_relevance_tier VARCHAR(16)"))
            if "outreach_score_explanation" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN outreach_score_explanation TEXT"))
            if "last_subject" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN last_subject TEXT"))
            if "last_preview" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN last_preview TEXT"))
            if "latest_outlook_weblink" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN latest_outlook_weblink TEXT"))

    # P6–P9 campaign columns (create_all won't ALTER existing campaigns table)
    if "campaigns" in inspector.get_table_names():
        camp_cols = {column["name"] for column in inspector.get_columns("campaigns")}
        with engine.begin() as conn:
            alters = [
                ("research_mode", "ALTER TABLE campaigns ADD COLUMN research_mode VARCHAR(32)"),
                ("message_strategy", "ALTER TABLE campaigns ADD COLUMN message_strategy JSON"),
                (
                    "external_research_status",
                    "ALTER TABLE campaigns ADD COLUMN external_research_status VARCHAR(32)",
                ),
                (
                    "external_research_progress",
                    "ALTER TABLE campaigns ADD COLUMN external_research_progress TEXT",
                ),
                (
                    "sending_account_id",
                    "ALTER TABLE campaigns ADD COLUMN sending_account_id VARCHAR(32)",
                ),
                (
                    "sending_account_confirmed_at",
                    "ALTER TABLE campaigns ADD COLUMN sending_account_confirmed_at DATETIME",
                ),
                (
                    "careers_justification",
                    "ALTER TABLE campaigns ADD COLUMN careers_justification TEXT",
                ),
            ]
            for name, sql in alters:
                if name not in camp_cols:
                    conn.execute(text(sql))

    if "campaign_candidates" in inspector.get_table_names():
        cand_cols = {column["name"] for column in inspector.get_columns("campaign_candidates")}
        with engine.begin() as conn:
            if "tracking_status" not in cand_cols:
                conn.execute(
                    text(
                        "ALTER TABLE campaign_candidates ADD COLUMN tracking_status VARCHAR(32)"
                    )
                )

    if "send_logs" in inspector.get_table_names():
        log_cols = {column["name"] for column in inspector.get_columns("send_logs")}
        with engine.begin() as conn:
            for name, sql in [
                (
                    "conversation_id",
                    "ALTER TABLE send_logs ADD COLUMN conversation_id VARCHAR(512)",
                ),
                (
                    "internet_message_id",
                    "ALTER TABLE send_logs ADD COLUMN internet_message_id VARCHAR(512)",
                ),
                (
                    "provider_message_id",
                    "ALTER TABLE send_logs ADD COLUMN provider_message_id VARCHAR(512)",
                ),
            ]:
                if name not in log_cols:
                    conn.execute(text(sql))

    # Speed indexes for the default contacts list scan
    if "contacts" in inspector.get_table_names():
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("contacts")}
        with engine.begin() as conn:
            if "ix_contacts_list_default" not in existing_indexes:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_contacts_list_default "
                        "ON contacts (is_internal, is_excluded, last_contacted_at)"
                    )
                )
            if "ix_contacts_review_list" not in existing_indexes:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_contacts_review_list "
                        "ON contacts (is_internal, is_excluded, review_status, last_contacted_at)"
                    )
                )


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_migrations()
    seed_mailbox_accounts()
    logger.info("Database ready (%s)", settings.database_url.split("://", 1)[0])
    cleanup_orphaned_jobs()

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
