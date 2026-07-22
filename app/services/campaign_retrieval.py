"""P4 — retrieve candidates only after Gate 1 plan approval."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.campaign import (
    Campaign,
    CampaignCandidate,
    EvidenceItem,
    PlanVersion,
)
from app.models.contact import Contact, ContactEmailLink
from app.models.message import EmailMessage
from app.services.conversation_suppression import collect_extra_suppressions
from app.services.evidence_assembler import (
    build_why_text,
    drop_uncited_claims_from_why,
    gather_message_evidence,
    validate_evidence_items,
)
from app.services.goal_ranker import classify_contact
from app.services.careers_classifier import classify_careers_sender, is_recruiting_noise
from app.services.objective_parser import (
    DEFAULT_CANDIDATE_LIMIT,
    clamp_candidate_limit,
)

logger = logging.getLogger(__name__)


class Gate1NotApprovedError(Exception):
    pass


def _careers_only_noise(
    contact: Contact,
    evidence: list[dict[str, Any]],
    account_ids: list[str],
) -> bool:
    """G-02: when Careers is in scope, block recruiting volume from candidate pools."""
    if "careers" not in account_ids:
        return False
    sources = {e.get("source_account") for e in evidence}
    if sources and sources <= {"careers"}:
        subjects = [e.get("subject") or "" for e in evidence]
        cls = classify_careers_sender(
            email=contact.primary_email,
            name=contact.full_name,
            subjects=subjects,
            company=contact.company_name,
        )
        return is_recruiting_noise(cls)
    return False


def require_approved_plan(db: Session, campaign: Campaign) -> PlanVersion:
    if not campaign.current_plan_version_id:
        raise Gate1NotApprovedError("No search plan exists for this campaign")
    plan = db.get(PlanVersion, campaign.current_plan_version_id)
    if not plan or not plan.approved_at:
        raise Gate1NotApprovedError(
            "Search plan is not approved (Gate 1). Approve the plan before research."
        )
    return plan


def _contacts_in_scope(
    db: Session,
    *,
    account_ids: list[str],
    lookback_years: int,
    limit: int = 80,
) -> list[Contact]:
    cutoff = datetime.utcnow() - timedelta(days=365 * max(1, lookback_years))
    # Prefer contacts that have messages from scoped accounts
    contact_ids_q = (
        db.query(ContactEmailLink.contact_id)
        .join(EmailMessage, EmailMessage.id == ContactEmailLink.email_message_id)
        .filter(EmailMessage.source_account.in_(account_ids))
        .filter(EmailMessage.sent_datetime >= cutoff)
        .distinct()
    )
    scoped_ids = [row[0] for row in contact_ids_q.limit(500).all()]

    q = db.query(Contact).filter(
        Contact.is_internal.is_(False),
        Contact.is_excluded.is_(False),
    )
    if scoped_ids:
        q = q.filter(Contact.id.in_(scoped_ids))
    else:
        # Fallback: any external contact with recent activity (Edge-heavy DBs)
        q = q.filter(Contact.last_contacted_at.isnot(None))
        q = q.filter(Contact.last_contacted_at >= cutoff)

    return (
        q.order_by(
            Contact.fundraising_relevance_score.desc(),
            Contact.email_count.desc(),
        )
        .limit(limit)
        .all()
    )


def run_campaign_research(db: Session, campaign_id: str) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("Campaign not found")

    plan_row = require_approved_plan(db, campaign)
    plan = dict(plan_row.plan_json or {})
    account_ids = list(plan.get("account_ids") or campaign.account_ids or [])
    lookback = int(plan.get("lookback_years") or 5)
    candidate_limit = clamp_candidate_limit(
        plan.get("candidate_limit"), DEFAULT_CANDIDATE_LIMIT
    )
    # Pull a wider pool than the final cut so ranking has room to discriminate.
    pool_limit = max(100, candidate_limit * 3)

    campaign.research_status = "running"
    campaign.research_progress = "Reviewing relationship history…"
    campaign.research_started_at = datetime.utcnow()
    campaign.research_error = None
    campaign.status = "researching"
    db.commit()

    try:
        # Clear prior candidates for this run
        for old in list(campaign.candidates):
            db.delete(old)
        db.flush()

        contacts = _contacts_in_scope(
            db, account_ids=account_ids, lookback_years=lookback, limit=pool_limit
        )
        campaign.research_progress = (
            f"Found {len(contacts)} potentially relevant people, ranking…"
        )
        db.commit()

        extras = collect_extra_suppressions(plan)
        strategy = dict(campaign.message_strategy or {})
        extras.extend(collect_extra_suppressions(strategy))

        ranked: list[tuple[Contact, dict[str, Any], list[dict[str, Any]]]] = []
        for contact in contacts:
            evidence = validate_evidence_items(
                gather_message_evidence(
                    db,
                    contact,
                    account_ids=account_ids,
                    extra_suppressions=extras,
                )
            )
            if not evidence and (contact.email_count or 0) < 1:
                continue
            if _careers_only_noise(contact, evidence, account_ids):
                continue
            labels = classify_contact(contact, plan=plan, evidence=evidence)
            if "excluded_role" in (labels.get("flags") or []):
                continue
            if "functional" in (labels.get("flags") or []) and "careers" in account_ids:
                # Soft-exclude functional Careers relationships from top pool
                continue
            # Drop contacts whose only recent volume was suppressed noise
            if "blast_only_or_no_signal" in (labels.get("flags") or []) and not evidence:
                continue
            ranked.append((contact, labels, evidence))

        ranked.sort(key=lambda t: t[1]["rank_score"], reverse=True)
        top = ranked[:candidate_limit]

        for idx, (contact, labels, evidence) in enumerate(top, start=1):
            why = drop_uncited_claims_from_why(
                build_why_text(contact, evidence), evidence
            )
            # Source accounts from evidence
            sources = sorted(
                {e.get("source_account") or "edge" for e in evidence}
            ) or list(account_ids[:1])

            flags = list(labels.get("flags") or [])
            conf = labels.get("confidence_label")
            if conf:
                flags.append(f"confidence_{conf}")
            cand = CampaignCandidate(
                campaign_id=campaign.id,
                contact_id=contact.id,
                rank=idx,
                full_name=contact.full_name or contact.primary_email,
                email=contact.primary_email,
                company=contact.company_name,
                role_label=labels["role_label"],
                strength_label=labels["strength_label"],
                relevance_label=labels["relevance_label"],
                why_text=why,
                source_accounts=sources,
                decision="proposed",
                rank_score=labels["rank_score"],
                flags=flags,
            )
            db.add(cand)
            db.flush()
            for ev in evidence[:6]:
                db.add(
                    EvidenceItem(
                        candidate_id=cand.id,
                        kind=ev.get("kind") or "email",
                        occurred_at=ev.get("occurred_at"),
                        source_account=ev.get("source_account"),
                        direction=ev.get("direction"),
                        subject=ev.get("subject"),
                        summary=ev.get("summary"),
                        message_id=ev.get("message_id"),
                        outlook_weblink=ev.get("outlook_weblink"),
                        citation_ok=True,
                    )
                )

        campaign.research_status = "completed"
        campaign.research_progress = (
            f"Ready: {len(top)} candidates with citable evidence"
        )
        campaign.research_completed_at = datetime.utcnow()
        campaign.status = "reviewing_contacts"
        db.commit()
        db.refresh(campaign)
        return campaign
    except Gate1NotApprovedError:
        raise
    except Exception as exc:
        logger.exception("Campaign research failed")
        campaign.research_status = "failed"
        campaign.research_error = str(exc)
        campaign.research_progress = "Research failed"
        campaign.research_completed_at = datetime.utcnow()
        db.commit()
        raise
