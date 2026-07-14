from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ContactListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    list_number: int | None = None
    full_name: str | None
    primary_email: str
    company_name: str | None
    company_domain: str | None
    first_contacted_at: datetime | None
    last_contacted_at: datetime | None
    email_count: int
    thread_count: int
    fundraising_relevance_score: int
    fundraising_relevance_tier: str | None
    contact_type: str | None
    status: str
    review_status: str = "pending"
    notes: str | None
    awaiting_reply: bool = False
    days_since_outreach: int | None = None
    last_inbound_at: datetime | None = None
    outreach_relevance_score: int = 0
    outreach_relevance_tier: str | None = None
    outreach_score_explanation: str | None = None
    is_internal: bool
    is_personal_email: bool
    is_excluded: bool
    auto_context_short: str | None = None
    detected_topics: list[str] | None = None
    detected_role: str | None = None
    ai_seniority: dict | None = None
    ai_outreach_intelligence: dict | None = None
    last_subject: str | None = None
    last_preview: str | None = None
    latest_outlook_weblink: str | None = None
    latest_message_id: str | None = None
    has_ai_summary: bool = False
    has_outreach_intelligence: bool = False


class ContactDetail(ContactListItem):
    score_breakdown: dict | None = None
    auto_context_detailed: str | None = None
    last_meaningful_email_preview: str | None = None
    meaningful_previews: list[str] | None = None
    ai_summary: str | None = None
    ai_follow_up_draft: str | None = None
    ai_contact_classification: dict | None = None
    ai_summary_generated_at: datetime | None = None


class ContactUpdate(BaseModel):
    notes: str | None = None
    status: str | None = None
    contact_type: str | None = None
    company_name: str | None = None
    review_status: str | None = None


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sync_type: str
    status: str
    messages_fetched: int
    messages_new: int
    contacts_updated: int
    checkpoint_url: str | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None


class AuthStatus(BaseModel):
    connected: bool
    user_email: str | None = None
    expires_at: datetime | None = None
    can_send_mail: bool = False
    token_scopes: list[str] = []


class StatsOut(BaseModel):
    total_contacts: int
    external_contacts: int
    high_relevance_contacts: int
    total_messages: int
    synced_messages: int
    graph_sent_total: int | None = None
    sync_complete: bool | None = None
    review_pending: int
    review_approved: int
    review_denied: int
    last_sync_at: datetime | None
