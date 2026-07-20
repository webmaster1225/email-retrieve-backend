"""P6–P9 Compass research, drafting, save, send tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setenv("FEATURE_COMPASS_CAMPAIGNS", "true")
    monkeypatch.setenv("FEATURE_COMPASS_SEND", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("RESEARCH_PROVIDER", "stub")
    monkeypatch.setenv("LINKEDIN_SIGNATURE_URL", "https://linkedin.com/in/example")
    from app.config import get_settings

    get_settings.cache_clear()

    from app.database import Base
    import app.models  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()
    get_settings.cache_clear()


def _seed_included_campaign(db):
    from app.models.campaign import (
        Campaign,
        CampaignCandidate,
        EvidenceItem,
        PlanVersion,
    )

    campaign = Campaign(
        title="Test",
        objective_raw="Help with Northwyn raise",
        objective_parsed={},
        status="reviewing_contacts",
        account_ids=["northwyn"],
        research_mode="light",
        message_strategy={"notes": "Warm tone", "ask": "15 minutes?"},
    )
    db.add(campaign)
    db.flush()
    pv = PlanVersion(
        campaign_id=campaign.id,
        version=1,
        plan_json={"account_ids": ["northwyn"]},
        approved_at=datetime.utcnow(),
        approved_by="user",
    )
    db.add(pv)
    db.flush()
    campaign.current_plan_version_id = pv.id
    cand = CampaignCandidate(
        campaign_id=campaign.id,
        rank=1,
        full_name="Sarah Chen",
        email="sarah@meridian.example",
        company="Meridian",
        role_label="investor",
        why_text="You exchanged several emails.",
        decision="include",
        source_accounts=["northwyn"],
    )
    db.add(cand)
    db.flush()
    db.add(
        EvidenceItem(
            candidate_id=cand.id,
            kind="email",
            occurred_at=datetime.utcnow() - timedelta(days=40),
            source_account="northwyn",
            direction="outbound",
            subject="Fund chat",
            summary="Discussed timing",
            message_id="msg-1",
            citation_ok=True,
        )
    )
    db.commit()
    return campaign, cand


def test_relationship_only_never_calls_provider(db_session):
    from app.services.research import RelationshipOnlyProvider, run_external_research
    from app.services.research.provider import StubResearchProvider

    campaign, _ = _seed_included_campaign(db_session)
    campaign.research_mode = "relationship_only"
    db_session.commit()
    stub = StubResearchProvider()
    run_external_research(db_session, campaign.id, provider=stub)
    assert stub.call_count == 0
    db_session.refresh(campaign)
    assert campaign.external_research_status == "completed"


def test_date_discipline_rejects_old_event_with_fresh_pub():
    from app.services.research.pipeline import event_recency_ok

    now = datetime(2026, 7, 1)
    assert not event_recency_ok(
        event_date=datetime(2020, 1, 1),
        publication_date=datetime(2026, 6, 1),
        now=now,
    )
    assert event_recency_ok(
        event_date=datetime(2026, 1, 15),
        publication_date=datetime(2026, 6, 1),
        now=now,
    )


def test_identity_unconfirmed_facts_suppressed(db_session):
    from app.models.campaign import CampaignCandidate
    from app.services.research.pipeline import hits_to_proposed_facts
    from app.services.research.provider import RawHit

    cand = CampaignCandidate(
        id="c1",
        campaign_id="x",
        full_name="Sarah Chen",
        company="Meridian",
        email="sarah@meridian.example",
    )
    hits = [
        RawHit(
            title="Random person news",
            url="https://example.com/x",
            snippet="Someone else did something in 2025.",
            publication_date=datetime(2025, 8, 1),
            match_signals=[],  # no name/org
        )
    ]
    props = hits_to_proposed_facts(cand, hits, now=datetime(2026, 7, 1))
    assert props
    assert props[0]["identity_confirmed"] is False
    assert props[0]["status"] == "rejected"


def test_unapproved_fact_blocked_from_draft_provenance(db_session):
    from app.models.campaign import ExternalFact
    from app.services.campaign_drafting import assert_provenance_facts_allowed

    campaign, cand = _seed_included_campaign(db_session)
    fact = ExternalFact(
        campaign_id=campaign.id,
        candidate_id=cand.id,
        claim="Closed Fund IV",
        sources=[],
        status="rejected",
        identity_confirmed=True,
    )
    db_session.add(fact)
    db_session.commit()
    with pytest.raises(ValueError):
        assert_provenance_facts_allowed(
            db_session, campaign.id, {"fact_ids": [fact.id]}
        )


def test_draft_generate_skips_rejected_public_facts(db_session):
    from app.models.campaign import ExternalFact
    from app.services.campaign_drafting import generate_campaign_drafts

    campaign, cand = _seed_included_campaign(db_session)
    db_session.add(
        ExternalFact(
            campaign_id=campaign.id,
            candidate_id=cand.id,
            claim="SECRET SHOULD NOT APPEAR",
            sources=[],
            status="rejected",
            identity_confirmed=True,
        )
    )
    db_session.commit()
    drafts = asyncio.run(generate_campaign_drafts(db_session, campaign.id))
    assert len(drafts) == 1
    assert "SECRET SHOULD NOT APPEAR" not in drafts[0].body
    assert drafts[0].provenance.get("fact_ids") == []


def test_banned_phrasing_lint():
    from app.services.campaign_drafting import lint_banned_phrases

    assert lint_banned_phrases("Hi — I researched you online.")
    assert not lint_banned_phrases("Hope you're well after our last chat.")


def test_save_without_sending_account_409(db_session):
    from app.services.campaign_send import SendGateError, save_drafts_to_mailbox

    campaign, _ = _seed_included_campaign(db_session)
    with pytest.raises(SendGateError) as exc:
        asyncio.run(save_drafts_to_mailbox(db_session, campaign.id))
    assert exc.value.status_code == 409


def test_careers_requires_justification(db_session):
    from app.services.campaign_send import SendGateError, confirm_sending_account

    campaign, _ = _seed_included_campaign(db_session)
    with pytest.raises(SendGateError) as exc:
        confirm_sending_account(db_session, campaign, account_id="careers")
    assert exc.value.status_code == 400


def test_gate8_red_team_requires_confirm_and_flag(db_session, monkeypatch):
    from app.models.campaign import CampaignDraft
    from app.services.campaign_send import SendGateError, authorize_send, confirm_sending_account

    campaign, cand = _seed_included_campaign(db_session)
    confirm_sending_account(db_session, campaign, account_id="northwyn")
    draft = CampaignDraft(
        campaign_id=campaign.id,
        candidate_id=cand.id,
        subject="Hi",
        body="Hello",
        status="approved",
    )
    db_session.add(draft)
    db_session.commit()

    with pytest.raises(SendGateError) as exc:
        asyncio.run(
            authorize_send(
                db_session,
                campaign.id,
                confirm=False,
                recipient_emails=["sarah@meridian.example"],
            )
        )
    assert exc.value.status_code == 400

    monkeypatch.setenv("FEATURE_COMPASS_SEND", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(SendGateError) as exc2:
        asyncio.run(
            authorize_send(
                db_session,
                campaign.id,
                confirm=True,
                recipient_emails=["sarah@meridian.example"],
            )
        )
    assert exc2.value.status_code == 403
    get_settings.cache_clear()


def test_send_recipient_mismatch(db_session):
    from app.models.campaign import CampaignDraft
    from app.services.campaign_send import SendGateError, authorize_send, confirm_sending_account

    campaign, cand = _seed_included_campaign(db_session)
    confirm_sending_account(db_session, campaign, account_id="northwyn")
    db_session.add(
        CampaignDraft(
            campaign_id=campaign.id,
            candidate_id=cand.id,
            subject="Hi",
            body="Hello",
            status="approved",
        )
    )
    db_session.commit()
    with pytest.raises(SendGateError) as exc:
        asyncio.run(
            authorize_send(
                db_session,
                campaign.id,
                confirm=True,
                recipient_emails=["wrong@example.com"],
            )
        )
    assert exc.value.status_code == 400
