"""P7/P9 polish + P10 tracking + P11 follow-ups tests."""

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
    monkeypatch.setenv("FEATURE_COMPASS_TRACKING", "true")
    monkeypatch.setenv("FEATURE_COMPASS_FOLLOWUPS", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("RESEARCH_PROVIDER", "stub")
    monkeypatch.setenv("LINKEDIN_SIGNATURE_URL", "https://linkedin.com/in/example")
    monkeypatch.setenv("FOLLOWUP_NO_RESPONSE_DAYS", "7")
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


def _seed(db):
    from app.models.campaign import Campaign, CampaignCandidate, EvidenceItem, PlanVersion

    campaign = Campaign(
        title="Track me",
        objective_raw="Northwyn raise",
        objective_parsed={},
        status="reviewing_drafts",
        account_ids=["northwyn"],
        research_mode="relationship_only",
        message_strategy={"ask": "15 minutes?", "notes": "warm"},
        sending_account_id="northwyn",
        sending_account_confirmed_at=datetime.utcnow(),
    )
    db.add(campaign)
    db.flush()
    pv = PlanVersion(
        campaign_id=campaign.id,
        version=1,
        plan_json={},
        approved_at=datetime.utcnow(),
        approved_by="user",
    )
    db.add(pv)
    db.flush()
    campaign.current_plan_version_id = pv.id
    cands = []
    for i, (name, email) in enumerate(
        [
            ("Sarah Chen", "sarah@meridian.example"),
            ("Raj Patel", "raj@atlas.example"),
            ("Noise Person", "noise@other.example"),
        ]
    ):
        cand = CampaignCandidate(
            campaign_id=campaign.id,
            rank=i + 1,
            full_name=name,
            email=email,
            company="Co",
            role_label="investor",
            why_text="past emails",
            decision="include",
            source_accounts=["northwyn"],
            tracking_status="sent",
        )
        db.add(cand)
        db.flush()
        db.add(
            EvidenceItem(
                candidate_id=cand.id,
                kind="email",
                subject="Catch up",
                summary="Prior thread",
                citation_ok=True,
                occurred_at=datetime.utcnow() - timedelta(days=40),
            )
        )
        cands.append(cand)
    db.commit()
    return campaign, cands


def test_variants_and_remove_public_refs(db_session):
    from app.models.campaign import ExternalFact
    from app.services.campaign_drafting import (
        generate_campaign_drafts,
        remove_public_refs,
        set_draft_variant,
    )

    campaign, cands = _seed(db_session)
    fact = ExternalFact(
        campaign_id=campaign.id,
        candidate_id=cands[0].id,
        claim="Closed Fund IV",
        sources=[{"url": "https://example.com"}],
        status="approved",
        identity_confirmed=True,
        event_date=datetime.utcnow() - timedelta(days=10),
        publication_date=datetime.utcnow(),
    )
    db_session.add(fact)
    db_session.commit()

    drafts = asyncio.run(generate_campaign_drafts(db_session, campaign.id))
    assert drafts
    d0 = drafts[0]
    assert d0.variant == "email"
    assert "linkedin.com/in/example" in (d0.body or "").lower()

    linked = asyncio.run(set_draft_variant(db_session, campaign.id, d0.id, "linkedin"))
    assert linked.variant == "linkedin"

    call = asyncio.run(set_draft_variant(db_session, campaign.id, d0.id, "call_script"))
    assert call.variant == "call_script"
    assert "ASK" in (call.body or "")

    email = asyncio.run(set_draft_variant(db_session, campaign.id, d0.id, "email"))
    email.provenance = {
        "evidence_ids": [],
        "fact_ids": [fact.id],
        "chips": [{"kind": "public", "label": "Closed Fund IV", "id": fact.id}],
    }
    email.body = (email.body or "") + " Congrats on the recent development — Closed Fund IV."
    db_session.commit()
    cleaned = remove_public_refs(db_session, campaign.id, email.id)
    assert cleaned.provenance.get("fact_ids") == []
    assert "Congrats on the recent development" not in (cleaned.body or "")


def test_schedule_worker_sends_due(db_session, monkeypatch):
    from app.models.campaign import CampaignDraft, SendLog
    from app.services import campaign_send

    campaign, cands = _seed(db_session)
    draft = CampaignDraft(
        campaign_id=campaign.id,
        candidate_id=cands[0].id,
        subject="Hello",
        body="Body",
        status="approved",
        lifecycle="scheduled",
        variant="email",
    )
    db_session.add(draft)
    db_session.flush()
    log = SendLog(
        campaign_id=campaign.id,
        draft_id=draft.id,
        candidate_id=cands[0].id,
        account_id="northwyn",
        recipient=cands[0].email,
        subject="Hello",
        body_hash="x",
        action="scheduled",
        scheduled_for=datetime.utcnow() - timedelta(minutes=1),
        authorized_at=datetime.utcnow(),
    )
    db_session.add(log)
    db_session.commit()

    async def fake_send(*args, **kwargs):
        return {
            "id": "msg-1",
            "conversation_id": "conv-abc",
            "internet_message_id": "<mid@example>",
        }

    monkeypatch.setattr(campaign_send, "_send_provider_mail", fake_send)
    n = asyncio.run(campaign_send.process_due_scheduled(db_session))
    assert n == 1
    db_session.refresh(log)
    assert log.action == "sent"
    assert log.conversation_id == "conv-abc"


def test_reply_matcher_precision(db_session):
    """≥20 sent logs + mixed inbound; precision >95% (no false positives)."""
    from app.models.campaign import SendLog
    from app.models.message import EmailMessage
    from app.services.campaign_tracking import match_inbound_to_send_log, refresh_campaign_tracking

    campaign, cands = _seed(db_session)
    sarah, raj, noise = cands

    for i in range(20):
        cand = sarah if i % 2 == 0 else raj
        db_session.add(
            SendLog(
                campaign_id=campaign.id,
                candidate_id=cand.id,
                account_id="northwyn",
                recipient=cand.email,
                subject=f"Reconnecting wave {i // 2}",
                body_hash=f"h{i}",
                action="sent",
                sent_at=datetime.utcnow() - timedelta(days=3),
                conversation_id=f"conv-{cand.id}-{i // 2}",
                authorized_at=datetime.utcnow(),
            )
        )
    db_session.commit()

    true_positives = 0
    false_positives = 0
    for i in range(10):
        cand = sarah if i % 2 == 0 else raj
        msg = EmailMessage(
            graph_message_id=f"g-true-{i}",
            conversation_id=f"conv-{cand.id}-{i // 2}",
            sent_datetime=datetime.utcnow() - timedelta(days=1),
            subject=f"Re: Reconnecting wave {i // 2}",
            body_preview="Happy to intro you to our partner.",
            sender_email=cand.email,
            direction="inbound",
            source_account="northwyn",
            raw_to=[],
        )
        db_session.add(msg)
        db_session.flush()
        log, how = match_inbound_to_send_log(db_session, msg, campaign_id=campaign.id)
        if log and log.candidate_id == cand.id:
            true_positives += 1

    for i in range(15):
        msg = EmailMessage(
            graph_message_id=f"g-noise-{i}",
            conversation_id=f"unrelated-{i}",
            sent_datetime=datetime.utcnow() - timedelta(hours=i),
            subject="Invoice reminder",
            body_preview="Please pay",
            sender_email=noise.email if i % 2 else "random@elsewhere.example",
            direction="inbound",
            source_account="northwyn",
            raw_to=[],
        )
        db_session.add(msg)
        db_session.flush()
        log, how = match_inbound_to_send_log(db_session, msg, campaign_id=campaign.id)
        if log:
            false_positives += 1

    db_session.commit()
    assert false_positives == 0
    assert true_positives >= 9
    precision = true_positives / max(1, true_positives + false_positives)
    assert precision > 0.95

    dash = refresh_campaign_tracking(db_session, campaign.id)
    assert dash["counts"]["replied"] + dash["counts"]["intro_offered"] >= 1
    assert any(r["matched_by"] == "conversation_id" for r in dash["replies"])


def test_followup_gate9_no_autosend(db_session):
    from app.models.campaign import SendLog
    from app.services import campaign_followups
    from app.services.campaign_send import SendGateError

    campaign, cands = _seed(db_session)
    sarah = cands[0]
    sarah.tracking_status = "no_response"
    db_session.add(
        SendLog(
            campaign_id=campaign.id,
            candidate_id=sarah.id,
            account_id="northwyn",
            recipient=sarah.email,
            subject="Hi",
            body_hash="h",
            action="sent",
            sent_at=datetime.utcnow() - timedelta(days=10),
            authorized_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    proposed = campaign_followups.propose_followups(db_session, campaign.id)
    assert proposed
    assert all(p.status == "proposed" for p in proposed)
    sent_before = (
        db_session.query(SendLog)
        .filter(SendLog.campaign_id == campaign.id, SendLog.action == "sent")
        .count()
    )
    assert sent_before == 1

    fu = proposed[0]
    campaign_followups.set_followup_status(db_session, campaign.id, fu.id, "approved")
    db_session.refresh(fu)
    assert fu.status == "approved"
    assert fu.gate9_authorized_at is None

    with pytest.raises(SendGateError):
        asyncio.run(
            campaign_followups.authorize_followup_send(
                db_session, campaign.id, fu.id, confirm=False
            )
        )


def test_followup_send_requires_confirm_and_flag(db_session, monkeypatch):
    from app.config import get_settings
    from app.models.campaign import FollowUpProposal
    from app.services import campaign_followups, campaign_send
    from app.services.campaign_send import SendGateError

    campaign, cands = _seed(db_session)
    fu = FollowUpProposal(
        campaign_id=campaign.id,
        candidate_id=cands[0].id,
        kind="no_response",
        subject="Nudge",
        body="Hi",
        status="approved",
    )
    db_session.add(fu)
    db_session.commit()

    with pytest.raises(SendGateError) as no_confirm:
        asyncio.run(
            campaign_followups.authorize_followup_send(
                db_session, campaign.id, fu.id, confirm=False, recipient_email=cands[0].email
            )
        )
    assert no_confirm.value.status_code == 400

    monkeypatch.setenv("FEATURE_COMPASS_SEND", "false")
    get_settings.cache_clear()
    with pytest.raises(SendGateError) as proposals_only:
        asyncio.run(
            campaign_followups.authorize_followup_send(
                db_session, campaign.id, fu.id, confirm=True, recipient_email=cands[0].email
            )
        )
    assert proposals_only.value.status_code == 403

    monkeypatch.setenv("FEATURE_COMPASS_SEND", "true")
    get_settings.cache_clear()

    async def fake_send(*args, **kwargs):
        return {"id": "m1", "conversation_id": "c1", "internet_message_id": "i1"}

    monkeypatch.setattr(campaign_send, "_send_provider_mail", fake_send)

    campaign.sending_account_confirmed_at = None
    db_session.commit()
    with pytest.raises(SendGateError) as no_acct:
        asyncio.run(
            campaign_followups.authorize_followup_send(
                db_session, campaign.id, fu.id, confirm=True, recipient_email=cands[0].email
            )
        )
    assert no_acct.value.status_code == 409

    campaign.sending_account_confirmed_at = datetime.utcnow()
    db_session.commit()
    res = asyncio.run(
        campaign_followups.authorize_followup_send(
            db_session, campaign.id, fu.id, confirm=True, recipient_email=cands[0].email
        )
    )
    assert res["status"] == "sent"
