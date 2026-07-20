from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    graph_message_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(512), index=True)
    sent_datetime: Mapped[datetime] = mapped_column(DateTime, index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    subject_normalized: Mapped[str | None] = mapped_column(Text)
    body_preview: Mapped[str | None] = mapped_column(Text)
    outlook_weblink: Mapped[str | None] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    importance: Mapped[str | None] = mapped_column(String(32))
    categories: Mapped[list | None] = mapped_column(JSON)
    sender_email: Mapped[str | None] = mapped_column(String(320), index=True)
    raw_from: Mapped[dict | None] = mapped_column(JSON)
    raw_to: Mapped[list] = mapped_column(JSON, default=list)
    raw_cc: Mapped[list] = mapped_column(JSON, default=list)
    raw_bcc: Mapped[list] = mapped_column(JSON, default=list)
    direction: Mapped[str] = mapped_column(String(16), default="outbound", index=True)
    source_account: Mapped[str] = mapped_column(String(32), default="edge", index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    contact_links: Mapped[list["ContactEmailLink"]] = relationship(back_populates="message")


class ConversationThread(Base):
    __tablename__ = "conversation_threads"
    __table_args__ = (UniqueConstraint("conversation_id", "contact_id", name="uq_thread_contact"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(512), index=True)
    contact_id: Mapped[str] = mapped_column(String(36), ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_subject: Mapped[str | None] = mapped_column(Text)
    latest_preview: Mapped[str | None] = mapped_column(Text)
    latest_outlook_weblink: Mapped[str | None] = mapped_column(Text)
    detected_keywords: Mapped[list | None] = mapped_column(JSON)
    subjects_all: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    contact: Mapped["Contact"] = relationship(back_populates="threads")
