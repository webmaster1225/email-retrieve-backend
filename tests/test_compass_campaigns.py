"""P3–P5 Compass campaign tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


TEST_OBJECTIVES = [
    "Help with Northwyn's capital raise — find LPs and warm introducers",
    "Reconnect with key healthcare operators I haven't spoken to in a while",
    "Find customers for Galaxy's new product line in Ontario",
    "Identify advisors who know family offices",
    "Revive neglected Edge Investing relationships from the last 3 years",
    "Find partners for a clinic roll-up",
    "Raise capital for Northwyn Fund IV from existing LPs",
    "Get intros to hospital system procurement leads",
    "Find an expert on Canadian pharmacy regulation",
    "Reconnect with investors who passed last year",
    "Customer intros for Galaxy Pharma vendors",
    "Find bankers? No — exclude investment bankers for this campaign",
    "Look back 2 years for Northwyn acquisition partners",
    "Help me meet family office allocators",
    "Find operators who could join a board",
    "Warm intros to PE healthcare funds",
    "Revive dormant LP relationships",
    "Find counsel familiar with cross-border pharma deals",
    "Partners for a joint venture in specialty pharmacy",
    "Fundraising help — prioritize people who've introduced me before",
]


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setenv("FEATURE_COMPASS_CAMPAIGNS", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
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


def test_twenty_objectives_parse_with_max_three_questions():
    from app.services.objective_parser import parse_objective_heuristic

    for obj in TEST_OBJECTIVES:
        parsed = parse_objective_heuristic(obj, round_num=0)
        assert parsed["goal_type"]
        assert isinstance(parsed["target_roles"], list)
        assert len(parsed["clarifying_questions"]) <= 3
        assert "careers" not in (parsed.get("recommended_accounts") or [])
        assert parsed["lookback_years"] >= 1
        assert parsed["restatement"]


def test_create_campaign_and_gate1_blocks_research(db_session):
    from app.services import campaign_service
    from app.services.campaign_retrieval import Gate1NotApprovedError, require_approved_plan

    campaign = asyncio.run(
        campaign_service.create_campaign(
            db_session,
            objective="Help with Northwyn's capital raise",
            account_ids=["northwyn", "edge"],
        )
    )
    assert campaign.id
    assert campaign.objective_parsed
    assert campaign.current_plan_version_id

    with pytest.raises(Gate1NotApprovedError):
        require_approved_plan(db_session, campaign)

    campaign_service.approve_plan(db_session, campaign)
    plan = require_approved_plan(db_session, campaign)
    assert plan.approved_at is not None


def test_citation_validation_drops_uncited():
    from app.services.evidence_assembler import validate_evidence_items

    items = validate_evidence_items(
        [
            {"kind": "email", "summary": "hello", "message_id": None},
            {"kind": "email", "summary": "real", "message_id": "abc-123", "subject": "Hi"},
            {"kind": "email", "summary": "", "subject": "", "message_id": "x"},
        ]
    )
    assert len(items) == 1
    assert items[0]["message_id"] == "abc-123"


def test_research_endpoint_409_without_approval(db_session, monkeypatch):
    monkeypatch.setenv("FEATURE_COMPASS_CAMPAIGNS", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import get_db
    from app.services import campaign_service

    campaign = asyncio.run(
        campaign_service.create_campaign(
            db_session,
            objective="Find investors for Northwyn",
            account_ids=["northwyn"],
        )
    )

    def _override():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    client = TestClient(app)
    res = client.post(f"/api/v1/campaigns/{campaign.id}/research/start")
    assert res.status_code == 409
    res2 = client.get(f"/api/v1/campaigns/{campaign.id}/candidates")
    assert res2.status_code == 409
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_gate2_decisions_and_nl_ops(db_session):
    from app.models.campaign import CampaignCandidate
    from app.services import campaign_service
    from app.services.nl_list_ops import apply_nl_op, preview_nl_op

    campaign = asyncio.run(
        campaign_service.create_campaign(
            db_session,
            objective="Help with Northwyn fundraising",
            account_ids=["northwyn"],
        )
    )
    campaign_service.approve_plan(db_session, campaign)

    c1 = CampaignCandidate(
        campaign_id=campaign.id,
        rank=1,
        full_name="Alice Banker",
        email="a@bank.com",
        company="Big Bank",
        role_label="banker",
        strength_label="solid",
        relevance_label="low_relevance",
        why_text="Met once.",
        decision="proposed",
        flags=[],
    )
    c2 = CampaignCandidate(
        campaign_id=campaign.id,
        rank=2,
        full_name="Bob Stale",
        email="b@x.com",
        role_label="investor",
        strength_label="needs_reconnection",
        relevance_label="medium_relevance",
        why_text="Old thread.",
        decision="proposed",
        flags=["stale"],
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    campaign_service.record_decisions(
        db_session,
        campaign,
        [{"candidate_id": c1.id, "decision": "include"}],
    )
    db_session.refresh(c1)
    assert c1.decision == "include"

    preview = preview_nl_op(db_session, campaign.id, "drop bankers")
    assert preview.matched_count >= 1
    assert "banker" in preview.restatement.lower()
    apply_nl_op(db_session, campaign.id, preview)
    db_session.refresh(c1)
    assert c1.decision == "pass"

    stale_preview = preview_nl_op(db_session, campaign.id, "remove anyone stale >3 years")
    assert stale_preview.matched_count >= 1
    assert "I'll mark" in stale_preview.restatement or "stale" in stale_preview.restatement.lower()


def test_research_builds_candidates_with_evidence(db_session):
    from app.models.contact import Contact, ContactEmailLink
    from app.models.message import EmailMessage
    from app.services import campaign_service
    from app.services.campaign_retrieval import run_campaign_research

    contact = Contact(
        full_name="Sarah Chen",
        primary_email="sarah@meridian.example",
        company_name="Meridian",
        email_count=5,
        thread_count=2,
        fundraising_relevance_score=80,
        relationship_score=40,
        last_contacted_at=datetime.utcnow() - timedelta(days=30),
        first_contacted_at=datetime.utcnow() - timedelta(days=400),
        is_internal=False,
        is_excluded=False,
        contact_type="investor",
    )
    db_session.add(contact)
    db_session.flush()
    msg = EmailMessage(
        graph_message_id="g-msg-1",
        sent_datetime=datetime.utcnow() - timedelta(days=20),
        subject="Fund update",
        body_preview="Thanks for the intro last month",
        direction="outbound",
        source_account="northwyn",
        outlook_weblink="https://outlook.example/1",
    )
    db_session.add(msg)
    db_session.flush()
    db_session.add(
        ContactEmailLink(
            contact_id=contact.id,
            email_message_id=msg.id,
            recipient_type="to",
        )
    )
    db_session.commit()

    campaign = asyncio.run(
        campaign_service.create_campaign(
            db_session,
            objective="Help with Northwyn capital raise — find investors",
            account_ids=["northwyn"],
        )
    )
    campaign_service.approve_plan(db_session, campaign)
    run_campaign_research(db_session, campaign.id)
    db_session.refresh(campaign)
    assert campaign.research_status == "completed"
    assert len(campaign.candidates) >= 1
    cand = campaign.candidates[0]
    assert cand.why_text
    assert cand.evidence_items
    assert all(e.citation_ok and e.message_id for e in cand.evidence_items)
