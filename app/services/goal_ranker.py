"""P4 — goal-conditioned ranking labels (internal scores, prose labels for UI)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.models.contact import Contact


ROLE_TYPE_HINTS: dict[str, list[str]] = {
    "investor": ["investor", "lp", "fund", "capital", "venture"],
    "introducer": ["introduc"],
    "advisor": ["advisor", "counsel"],
    "banker": ["bank", "ib "],
    "operator": ["ceo", "founder", "coo", "operator", "executive"],
    "vendor": ["vendor", "supplier"],
    "customer": ["customer", "client"],
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


def classify_contact(
    contact: Contact,
    *,
    plan: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.utcnow()
    lookback = int(plan.get("lookback_years") or 5)
    cutoff = now - timedelta(days=365 * lookback)
    target_roles = list(plan.get("relationship_types") or plan.get("target_roles") or [])
    exclusions = [e.lower() for e in (plan.get("exclusions") or []) if e != "None stated"]

    last = contact.last_contacted_at
    stale = bool(last and last < now - timedelta(days=365 * 3))
    out_of_lookback = bool(last and last < cutoff)
    emails = contact.email_count or 0
    threads = contact.thread_count or 0
    strength_score = min(100, emails * 5 + threads * 3 + (contact.relationship_score or 0) * 0.3)
    relevance = _role_match(contact, target_roles)
    fund = contact.fundraising_relevance_score or 0
    outreach = contact.outreach_relevance_score or 0
    goal_score = relevance * 60 + min(40, fund * 0.25 + outreach * 0.2)

    flags: list[str] = []
    if stale:
        flags.append("stale")
    if out_of_lookback:
        flags.append("outside_lookback")
    if emails < 2 and threads < 2:
        flags.append("insufficient_evidence")
    if contact.contact_type and "recruit" in (contact.contact_type or "").lower():
        flags.append("functional")

    # Exclusion soft filter
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
    elif "insufficient_evidence" in flags:
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

    # Pick primary role label
    role_label = target_roles[0] if target_roles else (contact.contact_type or "contact")
    for role in target_roles:
        hints = ROLE_TYPE_HINTS.get(role, [role])
        blob = f"{contact.contact_type or ''} {contact.company_name or ''}".lower()
        if any(h in blob for h in hints) or (
            role == "investor" and fund >= 40
        ):
            role_label = role
            break

    weights = plan.get("priority_weights") or {}
    w_rel = float(weights.get("relationship_strength", 0.45))
    w_goal = float(weights.get("goal_relevance", 0.55))
    rank_score = strength_score * w_rel + goal_score * w_goal
    if "excluded_role" in flags or "outside_lookback" in flags:
        rank_score *= 0.35
    if "insufficient_evidence" in flags:
        rank_score *= 0.5

    return {
        "role_label": role_label,
        "strength_label": strength_label,
        "relevance_label": relevance_label,
        "rank_score": float(rank_score),
        "flags": flags,
    }
