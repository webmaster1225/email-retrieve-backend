from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


CAMPAIGN_STATUSES = {
    "draft",
    "clarifying",
    "plan_pending",
    "researching",
    "reviewing_contacts",
    "confirming",
    "external_research",
    "reviewing_facts",
    "drafting",
    "reviewing_drafts",
    "awaiting_send_account",
    "ready_to_save",
    "scheduled",
    "sending",
    "tracking",
    "completed",
    "archived",
}

TRACKING_STATUSES = {
    "drafted",
    "saved",
    "scheduled",
    "sent",
    "replied",
    "intro_offered",
    "meeting_booked",
    "declined",
    "no_response",
}


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str | None] = mapped_column(String(256))
    objective_raw: Mapped[str] = mapped_column(Text)
    objective_parsed: Mapped[dict | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    account_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    clarification_round: Mapped[int] = mapped_column(Integer, default=0)
    research_status: Mapped[str | None] = mapped_column(String(32))
    research_progress: Mapped[str | None] = mapped_column(Text)
    research_error: Mapped[str | None] = mapped_column(Text)
    research_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    research_completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    current_plan_version_id: Mapped[str | None] = mapped_column(String(36))
    # P6+
    research_mode: Mapped[str | None] = mapped_column(String(32), default="relationship_only")
    message_strategy: Mapped[dict | None] = mapped_column(JSON, default=dict)
    external_research_status: Mapped[str | None] = mapped_column(String(32))
    external_research_progress: Mapped[str | None] = mapped_column(Text)
    # P8
    sending_account_id: Mapped[str | None] = mapped_column(String(32))
    sending_account_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    careers_justification: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    plan_versions: Mapped[list["PlanVersion"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    candidates: Mapped[list["CampaignCandidate"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    audit_events: Mapped[list["AuditEvent"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    external_facts: Mapped[list["ExternalFact"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    drafts: Mapped[list["CampaignDraft"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    send_logs: Mapped[list["SendLog"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    replies: Mapped[list["CampaignReply"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    commitments: Mapped[list["CampaignCommitment"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    follow_ups: Mapped[list["FollowUpProposal"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class PlanVersion(Base):
    __tablename__ = "plan_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    plan_json: Mapped[dict | None] = mapped_column(JSON, default=dict)
    assumptions: Mapped[list | None] = mapped_column(JSON, default=list)
    revision_note: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="plan_versions")


class CampaignCandidate(Base):
    __tablename__ = "campaign_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer, default=0, index=True)
    full_name: Mapped[str | None] = mapped_column(String(512))
    email: Mapped[str | None] = mapped_column(String(320))
    company: Mapped[str | None] = mapped_column(String(512))
    role_label: Mapped[str | None] = mapped_column(String(128))
    strength_label: Mapped[str | None] = mapped_column(String(64))
    relevance_label: Mapped[str | None] = mapped_column(String(64))
    why_text: Mapped[str | None] = mapped_column(Text)
    source_accounts: Mapped[list | None] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    rank_score: Mapped[float] = mapped_column(Float, default=0.0)
    flags: Mapped[list | None] = mapped_column(JSON, default=list)
    # P10 per-contact tracking status
    tracking_status: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="candidates")
    evidence_items: Mapped[list["EvidenceItem"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    decisions: Mapped[list["CampaignDecision"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    external_facts: Mapped[list["ExternalFact"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    drafts: Mapped[list["CampaignDraft"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    replies: Mapped[list["CampaignReply"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    commitments: Mapped[list["CampaignCommitment"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    follow_ups: Mapped[list["FollowUpProposal"]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class EvidenceItem(Base):
    __tablename__ = "evidence_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_candidates.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="email")
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_account: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[str | None] = mapped_column(String(16))
    subject: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(36), index=True)
    outlook_weblink: Mapped[str | None] = mapped_column(Text)
    citation_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    candidate: Mapped["CampaignCandidate"] = relationship(back_populates="evidence_items")


class CampaignDecision(Base):
    __tablename__ = "campaign_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_candidates.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str] = mapped_column(String(16))
    instruction_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    candidate: Mapped["CampaignCandidate"] = relationship(back_populates="decisions")


class ExternalFact(Base):
    __tablename__ = "external_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_candidates.id", ondelete="CASCADE"), index=True
    )
    claim: Mapped[str] = mapped_column(Text)
    sources: Mapped[list | None] = mapped_column(JSON, default=list)
    publication_date: Mapped[datetime | None] = mapped_column(DateTime)
    event_date: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    confidence: Mapped[str | None] = mapped_column(String(16), default="Medium")
    status: Mapped[str] = mapped_column(
        String(16), default="proposed", index=True
    )  # proposed|approved|background|rejected
    identity_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    quarantined_reason: Mapped[str | None] = mapped_column(Text)
    recommended_use: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="external_facts")
    candidate: Mapped["CampaignCandidate"] = relationship(back_populates="external_facts")


class CampaignDraft(Base):
    __tablename__ = "campaign_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_candidates.id", ondelete="CASCADE"), index=True
    )
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default="generated", index=True
    )  # generated|edited|approved
    lifecycle: Mapped[str] = mapped_column(
        String(16), default="approved_pending", index=True
    )  # approved_pending|saved|scheduled|sent
    provenance: Mapped[dict | None] = mapped_column(JSON, default=dict)
    ask: Mapped[str | None] = mapped_column(Text)
    warnings: Mapped[list | None] = mapped_column(JSON, default=list)
    variant: Mapped[str] = mapped_column(String(32), default="email")
    sending_account_override: Mapped[str | None] = mapped_column(String(32))
    mailbox_draft_id: Mapped[str | None] = mapped_column(String(512))
    mailbox_draft_web_link: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="drafts")
    candidate: Mapped["CampaignCandidate"] = relationship(back_populates="drafts")


class SendLog(Base):
    __tablename__ = "send_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    draft_id: Mapped[str | None] = mapped_column(String(36), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), index=True)
    account_id: Mapped[str | None] = mapped_column(String(32))
    recipient: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(Text)
    body_hash: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(
        String(16), index=True
    )  # scheduled|sent|failed|cancelled
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    authorized_by: Mapped[str | None] = mapped_column(String(128))
    # P10 reply matching
    conversation_id: Mapped[str | None] = mapped_column(String(512), index=True)
    internet_message_id: Mapped[str | None] = mapped_column(String(512), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    campaign: Mapped["Campaign"] = relationship(back_populates="send_logs")


class CampaignReply(Base):
    __tablename__ = "campaign_replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_candidates.id", ondelete="CASCADE"), index=True
    )
    send_log_id: Mapped[str | None] = mapped_column(String(36), index=True)
    message_id: Mapped[str | None] = mapped_column(String(36), index=True)
    matched_by: Mapped[str | None] = mapped_column(String(64))
    excerpt: Mapped[str | None] = mapped_column(Text)
    matched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="replies")
    candidate: Mapped["CampaignCandidate"] = relationship(back_populates="replies")


class CampaignCommitment(Base):
    __tablename__ = "campaign_commitments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_candidates.id", ondelete="CASCADE"), index=True
    )
    reply_id: Mapped[str | None] = mapped_column(String(36), index=True)
    owner: Mapped[str] = mapped_column(String(16), default="theirs")  # theirs|ours
    text: Mapped[str] = mapped_column(Text)
    due_hint: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default="open", index=True
    )  # open|done|dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="commitments")
    candidate: Mapped["CampaignCandidate"] = relationship(back_populates="commitments")


class FollowUpProposal(Base):
    __tablename__ = "follow_up_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaign_candidates.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(
        String(32), default="no_response"
    )  # no_response|commitment_nudge|intro_task
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default="proposed", index=True
    )  # proposed|approved|rejected|sent|cancelled
    based_on_status: Mapped[str | None] = mapped_column(String(32))
    gate9_authorized_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="follow_ups")
    candidate: Mapped["CampaignCandidate"] = relationship(back_populates="follow_ups")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    narrative: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    campaign: Mapped["Campaign"] = relationship(back_populates="audit_events")
