"""P4 — build Gate-1 search plan cards from parsed objectives."""

from __future__ import annotations

import re
from typing import Any

from app.services.objective_parser import (
    DEFAULT_CANDIDATE_LIMIT,
    clamp_candidate_limit,
)

ACCOUNT_LABELS = {
    "edge": "Edge Investing",
    "galaxy": "Galaxy Pharmaceuticals",
    "careers": "Galaxy Careers",
    "northwyn": "Northwyn",
}


def build_plan(
    *,
    objective_raw: str,
    parsed: dict[str, Any],
    account_ids: list[str],
    revision_note: str | None = None,
) -> dict[str, Any]:
    lookback = int(parsed.get("lookback_years") or 5)
    candidate_limit = clamp_candidate_limit(
        parsed.get("candidate_limit"), DEFAULT_CANDIDATE_LIMIT
    )
    roles = list(parsed.get("target_roles") or [])
    exclusions = list(parsed.get("exclusions") or [])
    entity = parsed.get("beneficiary_entity")
    accounts = [a for a in account_ids if a in ACCOUNT_LABELS]
    if not accounts:
        accounts = list(parsed.get("recommended_accounts") or ["edge", "northwyn"])

    plan = {
        "objective": objective_raw,
        "restatement": parsed.get("restatement") or objective_raw,
        "goal_type": parsed.get("goal_type") or "other",
        "beneficiary_entity": entity,
        "mailboxes": [
            {"id": a, "label": ACCOUNT_LABELS.get(a, a)} for a in accounts
        ],
        "account_ids": accounts,
        "lookback_years": lookback,
        "date_range_label": f"Last {lookback} years",
        "candidate_limit": candidate_limit,
        "relationship_types": roles,
        "prioritization": (
            "Strong reciprocal relationships first, then goal relevance"
        ),
        "exclusions": exclusions or ["None stated"],
        "include_calendar": False,
        "include_attachments": False,
        "external_research_later": True,
        "external_research_note": (
            "External web research runs later only for contacts you approve (Gate 3)."
        ),
        "suppressed_subjects": list(parsed.get("suppressed_subjects") or []),
        "noise_filtering": (
            "Bid Invitation / Collaborative Proposal threads and automated mail "
            "(calendar, auto-replies, NDRs) are excluded from hooks and strength."
        ),
        "assumptions": list(parsed.get("assumptions") or []),
        "revision_note": revision_note,
    }
    return plan


def apply_plan_revision(plan: dict[str, Any], instruction: str) -> dict[str, Any]:
    """Lightweight NL plan revision (heuristic)."""
    out = dict(plan)
    text = (instruction or "").lower()
    note = instruction.strip()
    out["revision_note"] = note

    if "banker" in text and ("remove" in text or "drop" in text or "exclude" in text):
        excl = [e for e in (out.get("exclusions") or []) if e != "None stated"]
        if "bankers" not in excl:
            excl.append("bankers")
        out["exclusions"] = excl
        roles = [r for r in (out.get("relationship_types") or []) if r != "banker"]
        out["relationship_types"] = roles

    if "careers" in text and ("add" in text or "include" in text):
        accounts = list(out.get("account_ids") or [])
        if "careers" not in accounts:
            accounts.append("careers")
        out["account_ids"] = accounts
        out["mailboxes"] = [
            {"id": a, "label": ACCOUNT_LABELS.get(a, a)} for a in accounts
        ]

    for years in (1, 2, 3, 5, 7, 10):
        if f"{years} year" in text or f"last {years}" in text:
            out["lookback_years"] = years
            out["date_range_label"] = f"Last {years} years"
            break

    limit_match = re.search(
        r"\b(?:top|first|only|limit(?:\s+to)?|show|return|surface)\s+(\d{1,3})\s*"
        r"(?:people|contacts|candidates|results)?\b"
        r"|\b(\d{1,3})\s+(?:people|contacts|candidates|results)\b"
        r"|\bcandidate[_\s-]?limit\s*[:=]?\s*(\d{1,3})\b",
        text,
    )
    if limit_match:
        raw = limit_match.group(1) or limit_match.group(2) or limit_match.group(3)
        out["candidate_limit"] = clamp_candidate_limit(raw)

    if "no external" in text or "relationship only" in text or "relationship-only" in text:
        out["external_research_later"] = False
        out["external_research_note"] = "Relationship-only — no external web research."

    if "calendar" in text and ("include" in text or "add" in text):
        out["include_calendar"] = True

    assumptions = list(out.get("assumptions") or [])
    assumptions.append(f"Plan revised: {note}")
    out["assumptions"] = assumptions
    return out
