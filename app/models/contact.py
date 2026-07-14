from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    full_name: Mapped[str | None] = mapped_column(String(512))
    primary_email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(512))
    company_domain: Mapped[str | None] = mapped_column(String(320), index=True)
    phone_number: Mapped[str | None] = mapped_column(String(64))
    linkedin_url: Mapped[str | None] = mapped_column(String(512))
    first_contacted_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    email_count: Mapped[int] = mapped_column(Integer, default=0)
    thread_count: Mapped[int] = mapped_column(Integer, default=0)
    relationship_score: Mapped[int] = mapped_column(Integer, default=0)
    fundraising_relevance_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    fundraising_relevance_tier: Mapped[str | None] = mapped_column(String(16))
    score_breakdown: Mapped[dict | None] = mapped_column(JSON)
    contact_type: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="active")
    review_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    list_number: Mapped[int | None] = mapped_column(Integer, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_personal_email: Mapped[bool] = mapped_column(Boolean, default=False)
    is_excluded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(256))
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    awaiting_reply: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    days_since_outreach: Mapped[int | None] = mapped_column(Integer)
    outreach_relevance_score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    outreach_relevance_tier: Mapped[str | None] = mapped_column(String(16))
    outreach_score_explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    email_links: Mapped[list["ContactEmailLink"]] = relationship(back_populates="contact")
    threads: Mapped[list["ConversationThread"]] = relationship(back_populates="contact")
    context: Mapped["ContactContext | None"] = relationship(back_populates="contact", uselist=False)
    tags: Mapped[list["ContactTag"]] = relationship(back_populates="contact")
    email_drafts: Mapped[list["EmailDraft"]] = relationship(back_populates="contact")


class ContactEmailLink(Base):
    __tablename__ = "contact_email_links"
    __table_args__ = (
        UniqueConstraint("contact_id", "email_message_id", "recipient_type", name="uq_contact_message_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    contact_id: Mapped[str] = mapped_column(String(36), ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    email_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("email_messages.id", ondelete="CASCADE"), index=True
    )
    recipient_type: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    contact: Mapped["Contact"] = relationship(back_populates="email_links")
    message: Mapped["EmailMessage"] = relationship(back_populates="contact_links")


class ContactContext(Base):
    __tablename__ = "contact_context"

    contact_id: Mapped[str] = mapped_column(String(36), ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    auto_context_short: Mapped[str | None] = mapped_column(Text)
    auto_context_detailed: Mapped[str | None] = mapped_column(Text)
    last_meaningful_email_preview: Mapped[str | None] = mapped_column(Text)
    last_meaningful_message_id: Mapped[str | None] = mapped_column(String(36))
    detected_topics: Mapped[list | None] = mapped_column(JSON)
    detected_company: Mapped[str | None] = mapped_column(String(512))
    detected_role: Mapped[str | None] = mapped_column(String(256))
    meaningful_previews: Mapped[list | None] = mapped_column(JSON)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_follow_up_draft: Mapped[str | None] = mapped_column(Text)
    ai_contact_classification: Mapped[dict | None] = mapped_column(JSON)
    ai_seniority: Mapped[dict | None] = mapped_column(JSON)
    ai_relationship_analysis: Mapped[dict | None] = mapped_column(JSON)
    ai_outreach_intelligence: Mapped[dict | None] = mapped_column(JSON)
    ai_summary_generated_at: Mapped[datetime | None] = mapped_column(DateTime)
    ai_model_used: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    contact: Mapped["Contact"] = relationship(back_populates="context")


class ManualTag(Base):
    __tablename__ = "manual_tags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    color: Mapped[str | None] = mapped_column(String(16))


class ContactTag(Base):
    __tablename__ = "contact_tags"
    __table_args__ = (UniqueConstraint("contact_id", "tag_id", name="uq_contact_tag"),)

    contact_id: Mapped[str] = mapped_column(String(36), ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True)
    tag_id: Mapped[str] = mapped_column(String(36), ForeignKey("manual_tags.id", ondelete="CASCADE"), primary_key=True)

    contact: Mapped["Contact"] = relationship(back_populates="tags")
    tag: Mapped["ManualTag"] = relationship()


from app.models.message import EmailMessage  # noqa: E402

ContactEmailLink.message = relationship("EmailMessage", back_populates="contact_links")

from app.models.outreach import EmailDraft  # noqa: E402, F401
