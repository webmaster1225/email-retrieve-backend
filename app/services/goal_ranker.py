"""P4 — goal-conditioned ranking on de-noised relationship signal."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.models.contact import Contact
from app.services.conversation_mining import COMMITMENT_RE, original_text_volume


ROLE_TYPE_HINTS: dict[str, list[str]] = {
    "investor": ["investor", "lp", "fund", "capital", "venture", "family office"],
    "introducer": ["introduc"],
    "advisor": ["advisor", "counsel", "board"],
    "banker": ["bank", "ib "],
    "operator": ["ceo", "founder", "coo", "operator", "executive"],
    "vendor": ["vendor", "supplier"],
    "customer": ["customer", "client"],
}

# Objective → topical keywords for evidence/company matching
GOAL_TOPIC_HINTS: dict[str, list[str]] = {
    "fundraising": [
        "fund",
        "lp",
        "raise",
        "capital",
        "investor",
        "close",
        "allocation",
        "co-invest",
    ],
    "reconnect": ["catch up", "reconnect", "intro", "coffee", "call"],
    "revive": ["catch up", "reconnect", "dormant", "neglected"],
    "find_expert": ["expert", "regulation", "counsel", "advisor", "specialist"],
    "find_partners": ["partner", "joint venture", "co-invest", "acquisition", "roll-up"],
    "customer_intros": ["customer", "procurement", "buyer", "vendor", "sales"],
    "other": [],
}


def _role_match(contact: Contact, target_roles: list[str]) -> float:
    blob = " ".join(
        filter(
            None,
            [
                contact.contact_type or "",
                contact.company_name or "",
                contact.full_name or "",
                (contact.outreach_score_explanation or "")[:200],
            ],
        )
    ).lower()
    if not target_roles:
        return 0.4
    hits = 0
    for role in target_roles:
        hints = ROLE_TYPE_HINTS.get(role, [role])
        if any(h in blob for h in hints):
            hits += 1
        elif role == "investor" and (contact.fundraising_relevance_score or 0) >= 40:
            hits += 1
    return min(1.0, hits / max(1, len(target_roles)))


def _evidence_quality(evidence: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Compute de-noised exchange quality from already-filtered evidence items."""
    evidence = evidence or []
    if not evidence:
        return {
            "genuine_count": 0,
            "two_way": False,
            "substance_chars": 0,
            "commitment_hits": 0,
            "topic_blob": "",
            "quality_score": 0.0,
        }
    directions = {(e.get("direction") or "").lower() for e in evidence}
    two_way = "inbound" in directions and "outbound" in directions
    substance = 0
    commitments = 0
    parts: list[str] = []
    for e in evidence:
        preview = e.get("body_preview") or e.get("summary") or ""
        substance += original_text_volume(preview)
        blob = f"{e.get('subject') or ''} {e.get('summary') or ''} {preview}"
        parts.append(blob)
        if COMMITMENT_RE.search(blob):
            commitments += 1
    topic_blob = " ".join(parts).lower()
    quality = min(100.0, len(evidence) * 12.0 + (25.0 if two_way else 0.0))
    quality += min(20.0, substance / 40.0)
    quality += min(15.0, commitments * 5.0)
    return {
        "genuine_count": len(evidence),
        "two_way": two_way,
        "substance_chars": substance,
        "commitment_hits": commitments,
        "topic_blob": topic_blob,
        "quality_score": min(100.0, quality),
    }


def _goal_topic_bonus(goal_type: str, topic_blob: str, contact: Contact) -> float:
    hints = GOAL_TOPIC_HINTS.get(goal_type or "other", [])
    if not hints:
        return 0.0
    company_blob = f"{contact.company_name or ''} {contact.contact_type or ''}".lower()
    blob = f"{topic_blob} {company_blob}"
    hits = sum(1 for h in hints if h in blob)
    return min(25.0, hits * 6.0)


def confidence_from_quality(
    *,
    genuine_count: int,
    two_way: bool,
    quality_score: float,
) -> str:
    """Real confidence that varies — not uniform High boilerplate."""
    if genuine_count >= 4 and two_way and quality_score >= 55:
        return "high"
    if genuine_count >= 2 and quality_score >= 30:
        return "medium"
    return "low"


def classify_contact(
    contact: Contact,
    *,
    plan: dict[str, Any],
    now: datetime | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = now or datetime.utcnow()
    lookback = int(plan.get("lookback_years") or 5)
    cutoff = now - timedelta(days=365 * lookback)
    target_roles = list(plan.get("relationship_types") or plan.get("target_roles") or [])
    exclusions = [e.lower() for e in (plan.get("exclusions") or []) if e != "None stated"]
    goal_type = (plan.get("goal_type") or "other").lower()

    last = contact.last_contacted_at
    # Prefer last touch from de-noised evidence when available
    if evidence:
        times = [e.get("occurred_at") for e in evidence if isinstance(e.get("occurred_at"), datetime)]
        if times:
            last = max(times)

    stale = bool(last and last < now - timedelta(days=365 * 3))
    out_of_lookback = bool(last and last < cutoff)
    emails = contact.email_count or 0
    threads = contact.thread_count or 0

    eq = _evidence_quality(evidence)
    # Strength from de-noised quality; volume is a soft prior only
    volume_prior = min(35.0, emails * 2.0 + threads * 1.5)
    stored = (contact.relationship_score or 0) * 0.15
    strength_score = min(100.0, eq["quality_score"] * 0.75 + volume_prior * 0.35 + stored)

    # Blast-only / empty de-noised set: sink hard
    if evidence is not None and eq["genuine_count"] == 0:
        strength_score = min(strength_score, 12.0)
        flags_blast = True
    else:
        flags_blast = False
    if evidence is not None and eq["genuine_count"] <= 1 and not eq["two_way"]:
        strength_score *= 0.55

    relevance = _role_match(contact, target_roles)
    fund = contact.fundraising_relevance_score or 0
    outreach = contact.outreach_relevance_score or 0
    goal_score = relevance * 50 + min(30, fund * 0.2 + outreach * 0.15)
    goal_score += _goal_topic_bonus(goal_type, eq["topic_blob"], contact)

    # Objective-specific ranking nudges
    if goal_type in ("reconnect", "revive") and stale and eq["genuine_count"] >= 1:
        goal_score += 12.0
    if goal_type == "fundraising" and fund >= 40:
        goal_score += 8.0
    if goal_type == "find_expert" and any(
        h in (contact.contact_type or "").lower() for h in ("advisor", "counsel", "expert")
    ):
        goal_score += 10.0

    flags: list[str] = []
    if flags_blast:
        flags.append("blast_only_or_no_signal")
    if stale:
        flags.append("stale")
    if out_of_lookback:
        flags.append("outside_lookback")
    if eq["genuine_count"] < 2 and emails < 2 and threads < 2:
        flags.append("insufficient_evidence")
    if contact.contact_type and "recruit" in (contact.contact_type or "").lower():
        flags.append("functional")
    else:
        from app.services.careers_classifier import classify_careers_sender, is_recruiting_noise

        cls = classify_careers_sender(
            email=contact.primary_email,
            name=contact.full_name,
            company=contact.company_name,
        )
        if is_recruiting_noise(cls):
            flags.append("functional")
            flags.append(f"careers_{cls}")

    ctype = (contact.contact_type or "").lower()
    if "bankers" in exclusions and ("bank" in ctype or "banker" in ctype):
        flags.append("excluded_role")
    if "vendors" in exclusions and "vendor" in ctype:
        flags.append("excluded_role")

    if strength_score >= 40 and goal_score < 25:
        strength_label = "strong_relationship"
        relevance_label = "low_relevance"
    elif strength_score < 25 and goal_score >= 40:
        strength_label = "weak_relationship"
        relevance_label = "high_relevance"
    elif "insufficient_evidence" in flags or "blast_only_or_no_signal" in flags:
        strength_label = "insufficient_evidence"
        relevance_label = "unclear"
    elif "functional" in flags:
        strength_label = "functional"
        relevance_label = "low_relevance"
    elif stale:
        strength_label = "needs_reconnection"
        relevance_label = "medium_relevance" if goal_score >= 30 else "low_relevance"
    else:
        strength_label = "solid"
        relevance_label = "high_relevance" if goal_score >= 40 else "medium_relevance"

    conf = confidence_from_quality(
        genuine_count=eq["genuine_count"],
        two_way=eq["two_way"],
        quality_score=eq["quality_score"],
    )
    # Map confidence into relevance when evidence is thin
    if conf == "low" and relevance_label == "high_relevance":
        relevance_label = "medium_relevance"
    if conf == "high" and relevance_label == "medium_relevance" and goal_score >= 35:
        relevance_label = "high_relevance"

    role_label = target_roles[0] if target_roles else (contact.contact_type or "contact")
    for role in target_roles:
        hints = ROLE_TYPE_HINTS.get(role, [role])
        blob = f"{contact.contact_type or ''} {contact.company_name or ''}".lower()
        if any(h in blob for h in hints) or (role == "investor" and fund >= 40):
            role_label = role
            break

    weights = plan.get("priority_weights") or {}
    w_rel = float(weights.get("relationship_strength", 0.45))
    w_goal = float(weights.get("goal_relevance", 0.55))
    # Reconnect/revive: weight relationship history higher
    if goal_type in ("reconnect", "revive"):
        w_rel, w_goal = 0.6, 0.4
    elif goal_type == "fundraising":
        w_rel, w_goal = 0.4, 0.6
    elif goal_type == "find_expert":
        w_rel, w_goal = 0.35, 0.65

    rank_score = strength_score * w_rel + goal_score * w_goal
    if "excluded_role" in flags or "outside_lookback" in flags:
        rank_score *= 0.35
    if "insufficient_evidence" in flags:
        rank_score *= 0.5
    if "blast_only_or_no_signal" in flags:
        rank_score *= 0.25

    return {
        "role_label": role_label,
        "strength_label": strength_label,
        "relevance_label": relevance_label,
        "confidence_label": conf,
        "rank_score": float(rank_score),
        "flags": flags,
        "genuine_exchange_count": eq["genuine_count"],
        "two_way": eq["two_way"],
    }
