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
    logger.info("Database ready (%s)", settings.database_url.split("://", 1)[0])
    cleanup_orphaned_jobs()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
