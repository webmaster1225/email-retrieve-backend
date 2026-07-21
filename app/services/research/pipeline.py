"""External research pipeline — identity, sensitivity, date discipline, Gate 3 proposals."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.campaign import Campaign, CampaignCandidate, ExternalFact
from app.services.campaign_service import audit
from app.services.research.provider import (
    IdentitySignature,
    RawHit,
    ResearchProvider,
    RelationshipOnlyProvider,
    get_research_provider,
)

logger = logging.getLogger(__name__)

SENSITIVE_PATTERNS = re.compile(
    r"\b(cancer|divorce|pregnant|religion|democrat|republican|lgbt|orientation|"
    r"home address|ssn|lawsuit|layoff|bankrupt|scandal|rumor)\b",
    re.I,
)


def is_sensitive(text: str) -> bool:
    return bool(SENSITIVE_PATTERNS.search(text or ""))


def event_recency_ok(
    *,
    event_date: datetime | None,
    publication_date: datetime | None,
    now: datetime | None = None,
    max_age_days: int = 365,
) -> bool:
    """Recency judged on event_date, never publication_date alone."""
    now = now or datetime.utcnow()
    if event_date is None:
        return False
    if event_date > now + timedelta(days=1):
        return False
    return event_date >= now - timedelta(days=max_age_days)


def identity_signals(identity: IdentitySignature, hit: RawHit) -> list[str]:
    signals: list[str] = list(hit.match_signals or [])
    blob = f"{hit.title} {hit.snippet} {hit.url}".lower()
    if identity.name and identity.name.lower() in blob and "name" not in signals:
        signals.append("name")
    if identity.org and identity.org.lower() in blob and "org" not in signals:
        signals.append("org")
    if identity.domain and identity.domain.lower() in blob and "domain" not in signals:
        signals.append("domain")
    if identity.email and identity.email.split("@")[0].lower() in blob and "email_local" not in signals:
        signals.append("email_local")
    return signals


def extract_event_date(hit: RawHit) -> datetime | None:
    """Prefer explicit publication; try year in snippet as weak event date."""
    if hit.publication_date:
        # If snippet mentions an earlier year, prefer that as event_date
        m = re.search(r"\b(20\d{2})\b", hit.snippet or "")
        if m:
            year = int(m.group(1))
            if year < hit.publication_date.year:
                return datetime(year, 6, 1)
        return hit.publication_date
    m = re.search(r"\b(20\d{2})\b", f"{hit.title} {hit.snippet}")
    if m:
        return datetime(int(m.group(1)), 6, 1)
    return None


def hits_to_proposed_facts(
    candidate: CampaignCandidate,
    hits: list[RawHit],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or datetime.utcnow()
    identity = IdentitySignature(
        name=candidate.full_name,
        org=candidate.company,
        email=candidate.email,
        domain=(candidate.email or "").split("@")[-1] if candidate.email else None,
        title=candidate.role_label,
    )
    proposed: list[dict[str, Any]] = []
    for hit in hits:
        if is_sensitive(f"{hit.title} {hit.snippet}"):
            continue
        signals = identity_signals(identity, hit)
        identity_ok = len(set(signals)) >= 2
        event_date = extract_event_date(hit)
        pub = hit.publication_date
        if not identity_ok:
            proposed.append(
                {
                    "claim": hit.snippet[:280] or hit.title,
                    "sources": [
                        {
                            "title": hit.title,
                            "publisher": hit.publisher,
                            "url": hit.url,
                            "pub_date": pub.isoformat() if pub else None,
                            "event_date": event_date.isoformat() if event_date else None,
                            "retrieved_at": now.isoformat(),
                        }
                    ],
                    "publication_date": pub,
                    "event_date": event_date,
                    "confidence": "Low",
                    "identity_confirmed": False,
                    "quarantined_reason": "Identity not confirmed (≥2 signals required)",
                    "status": "rejected",
                    "recommended_use": "Not used — could not confirm this is the same person.",
                }
            )
            continue
        if not event_recency_ok(event_date=event_date, publication_date=pub, now=now):
            proposed.append(
                {
                    "claim": hit.snippet[:280] or hit.title,
                    "sources": [
                        {
                            "title": hit.title,
                            "publisher": hit.publisher,
                            "url": hit.url,
                            "pub_date": pub.isoformat() if pub else None,
                            "event_date": event_date.isoformat() if event_date else None,
                            "retrieved_at": now.isoformat(),
                        }
                    ],
                    "publication_date": pub,
                    "event_date": event_date,
                    "confidence": "Low",
                    "identity_confirmed": True,
                    "quarantined_reason": "Event date too old or missing (date discipline)",
                    "status": "rejected",
                    "recommended_use": "Not used — not recent enough by event date.",
                }
            )
            continue
        proposed.append(
            {
                "claim": hit.snippet[:280] or hit.title,
                "sources": [
                    {
                        "title": hit.title,
                        "publisher": hit.publisher,
                        "url": hit.url,
                        "pub_date": pub.isoformat() if pub else None,
                        "event_date": event_date.isoformat() if event_date else None,
                        "retrieved_at": now.isoformat(),
                    }
                ],
                "publication_date": pub,
                "event_date": event_date,
                "confidence": "High" if len(signals) >= 3 else "Medium",
                "identity_confirmed": True,
                "quarantined_reason": None,
                "status": "proposed",
                "recommended_use": "Optional one-line personalization if it improves the message.",
            }
        )
    return proposed


def run_external_research(
    db: Session,
    campaign_id: str,
    *,
    provider: ResearchProvider | None = None,
) -> Campaign:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("Campaign not found")

    mode = (campaign.research_mode or "relationship_only").lower()
    # Normalize UI aliases
    if mode in ("light_standard", "light-standard"):
        mode = "light"
    campaign.external_research_status = "running"
    campaign.external_research_progress = "Preparing external research…"
    campaign.status = "external_research"
    db.commit()

    # Clear prior proposed facts for re-run
    db.query(ExternalFact).filter(ExternalFact.campaign_id == campaign_id).delete()
    db.commit()

    if mode == "relationship_only":
        prov: ResearchProvider = RelationshipOnlyProvider()
        max_facts_per_person = 0
        dig_deeper = False
    else:
        # light / standard / enhanced share provider seam; depth differs
        prov = provider or get_research_provider()
        max_facts_per_person = {"light": 1, "standard": 3, "enhanced": 6}.get(mode, 2)
        dig_deeper = mode == "enhanced"

    included = (
        db.query(CampaignCandidate)
        .filter(
            CampaignCandidate.campaign_id == campaign_id,
            CampaignCandidate.decision == "include",
        )
        .order_by(CampaignCandidate.rank.asc())
        .all()
    )

    total_facts = 0
    for cand in included:
        campaign.external_research_progress = f"Checking public context for {cand.full_name}…"
        db.commit()
        identity = IdentitySignature(
            name=cand.full_name,
            org=cand.company,
            email=cand.email,
            domain=(cand.email or "").split("@")[-1] if cand.email else None,
            title=cand.role_label,
        )
        hits = prov.search_person(identity)
        if dig_deeper and hasattr(prov, "search_person"):
            # Enhanced: second pass with title-heavy query (same provider)
            alt = IdentitySignature(
                name=cand.full_name,
                org=cand.company,
                email=cand.email,
                domain=(cand.email or "").split("@")[-1] if cand.email else None,
                title=(cand.role_label or "") + " interview OR announcement",
            )
            hits = hits + prov.search_person(alt)
        proposals = hits_to_proposed_facts(cand, hits)
        if max_facts_per_person and len(proposals) > max_facts_per_person:
            proposals = proposals[:max_facts_per_person]
        if not proposals:
            # Honest empty result — no fabricated facts
            continue
        for p in proposals:
            if not p.get("identity_confirmed"):
                # Suppress from Gate 3 UI as usable — still store as rejected for audit
                pass
            fact = ExternalFact(
                campaign_id=campaign_id,
                candidate_id=cand.id,
                claim=p["claim"],
                sources=p["sources"],
                publication_date=p.get("publication_date"),
                event_date=p.get("event_date"),
                retrieved_at=datetime.utcnow(),
                confidence=p.get("confidence"),
                status=p.get("status") or "proposed",
                identity_confirmed=bool(p.get("identity_confirmed")),
                quarantined_reason=p.get("quarantined_reason"),
                recommended_use=p.get("recommended_use"),
            )
            # Only surface proposed/identity-ok to Gate 3 as proposed
            if fact.identity_confirmed and fact.status == "proposed":
                total_facts += 1
            elif not fact.identity_confirmed:
                fact.status = "rejected"
            db.add(fact)

    campaign.external_research_status = "completed"
    campaign.external_research_progress = (
        f"External research done — {total_facts} fact(s) for review"
        if total_facts
        else "No public facts to add — drafts will use relationship history only"
    )
    campaign.status = "reviewing_facts"
    audit(
        db,
        campaign_id,
        "external_research_completed",
        campaign.external_research_progress or "",
        {"mode": mode, "provider": getattr(prov, "name", "unknown"), "facts": total_facts},
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def facts_usable_in_drafts(db: Session, campaign_id: str) -> list[ExternalFact]:
    """Server gate: only approved (or background-only as soft) facts reach drafting."""
    return (
        db.query(ExternalFact)
        .filter(
            ExternalFact.campaign_id == campaign_id,
            ExternalFact.status.in_(("approved", "background")),
            ExternalFact.identity_confirmed.is_(True),
        )
        .all()
    )
