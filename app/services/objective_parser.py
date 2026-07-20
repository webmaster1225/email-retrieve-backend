"""P3 — parse free-text objectives into structured search criteria."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

VALID_GOAL_TYPES = {
    "fundraising",
    "customer_intros",
    "reconnect",
    "find_expert",
    "find_partners",
    "revive",
    "other",
}

ACCOUNT_HINTS: dict[str, list[str]] = {
    "northwyn": ["northwyn", "nw ", "capital raise", "fundraising", "fund", "lp ", "investor"],
    "edge": ["edge investing", "edge ", "capital markets", "deal", "m&a", "banker"],
    "galaxy": ["galaxy", "pharma", "pharmacy", "clinic", "healthcare", "vendor"],
    "careers": ["recruiting", "hiring", "candidate", "agency", "careers@"],
}


def empty_parsed() -> dict[str, Any]:
    return {
        "goal_type": "other",
        "beneficiary_entity": None,
        "target_roles": [],
        "geo": None,
        "exclusions": [],
        "lookback_years": 5,
        "priority_weights": {"relationship_strength": 0.45, "goal_relevance": 0.55},
        "recommended_accounts": ["edge", "northwyn"],
        "assumptions": [],
        "clarifying_questions": [],
        "restatement": "",
    }


def _recommend_accounts(text: str) -> list[str]:
    lower = text.lower()
    scores: dict[str, int] = {k: 0 for k in ACCOUNT_HINTS}
    for account_id, hints in ACCOUNT_HINTS.items():
        for hint in hints:
            if hint in lower:
                scores[account_id] += 1
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    chosen = [aid for aid, sc in ranked if sc > 0 and aid != "careers"]
    if not chosen:
        chosen = ["edge", "northwyn"]
    # Careers never default-on
    return chosen[:3]


def _infer_goal_type(text: str) -> str:
    lower = text.lower()
    rules = [
        ("fundraising", ["raise", "fundraising", "capital", "investor", "lp ", "fund"]),
        ("customer_intros", ["customer", "client intro", "sales", "buyer"]),
        ("reconnect", ["reconnect", "catch up", "reach out again", "re-engage"]),
        ("find_expert", ["expert", "advisor", "specialist", "counsel"]),
        ("find_partners", ["partner", "joint venture", "co-invest", "acquisition partner"]),
        ("revive", ["neglected", "stale", "haven't spoken", "dormant"]),
    ]
    for goal, keys in rules:
        if any(k in lower for k in keys):
            return goal
    return "other"


def _infer_roles(text: str, goal_type: str) -> list[str]:
    lower = text.lower()
    roles: list[str] = []
    role_map = [
        ("investor", ["investor", "lp", "family office", "fund"]),
        ("introducer", ["introduc", "warm intro", "connector"]),
        ("advisor", ["advisor", "counsel", "mentor"]),
        ("banker", ["banker", "investment bank"]),
        ("operator", ["operator", "executive", "ceo", "founder"]),
        ("vendor", ["vendor", "supplier"]),
        ("customer", ["customer", "client"]),
    ]
    for role, keys in role_map:
        if any(k in lower for k in keys):
            roles.append(role)
    if not roles:
        defaults = {
            "fundraising": ["investor", "introducer", "advisor"],
            "customer_intros": ["customer", "introducer"],
            "reconnect": ["operator", "advisor"],
            "find_expert": ["advisor", "operator"],
            "find_partners": ["operator", "investor"],
            "revive": ["operator", "introducer"],
        }
        roles = defaults.get(goal_type, ["operator", "introducer"])
    return roles


def _infer_exclusions(text: str) -> list[str]:
    lower = text.lower()
    exclusions: list[str] = []
    if "banker" in lower and ("not" in lower or "except" in lower or "exclude" in lower or "no " in lower):
        exclusions.append("bankers")
    if "recruit" in lower or "candidate" in lower:
        exclusions.append("recruiting_volume")
    if re.search(r"not\s+vendor|no\s+vendor|exclude\s+vendor", lower):
        exclusions.append("vendors")
    return exclusions


def _infer_entity(text: str) -> str | None:
    for name in ("Northwyn", "Edge Investing", "Galaxy", "Galaxy Pharma"):
        if name.lower() in text.lower():
            return name
    m = re.search(r"\bfor\s+([A-Z][A-Za-z0-9& ]{1,40})", text)
    if m:
        return m.group(1).strip()
    return None


def _clarifying_questions(parsed: dict[str, Any], round_num: int) -> list[str]:
    if round_num >= 3:
        return []
    questions: list[str] = []
    if not parsed.get("geo"):
        questions.append("Any geography focus, or open to anywhere?")
    if parsed.get("goal_type") == "fundraising" and "amount" not in " ".join(
        parsed.get("assumptions") or []
    ).lower():
        questions.append("Roughly how much are you raising, and what can I disclose?")
    if not parsed.get("exclusions"):
        questions.append("Anyone to exclude — e.g. bankers, vendors, or people contacted recently?")
    return questions[: max(0, 3 - round_num)]


def parse_objective_heuristic(objective: str, *, round_num: int = 0) -> dict[str, Any]:
    """Deterministic parser used in tests and when Anthropic is unavailable."""
    text = (objective or "").strip()
    parsed = empty_parsed()
    if not text:
        parsed["clarifying_questions"] = [
            "What would you like to accomplish through your relationships?"
        ]
        parsed["restatement"] = "I need a short objective to get started."
        return parsed

    goal = _infer_goal_type(text)
    entity = _infer_entity(text)
    roles = _infer_roles(text, goal)
    exclusions = _infer_exclusions(text)
    accounts = _recommend_accounts(text)
    lookback = 5
    if re.search(r"\b(2|two)\s+years?\b", text.lower()):
        lookback = 2
    elif re.search(r"\b(3|three)\s+years?\b", text.lower()):
        lookback = 3
    elif re.search(r"\b(1|one)\s+year\b", text.lower()):
        lookback = 1

    assumptions = [
        f"Look back ~{lookback} years of relationship history",
        "Prioritize people with reciprocal email history",
        "Careers mailbox stays excluded unless you add it",
    ]
    if entity:
        assumptions.append(f"Beneficiary entity: {entity}")

    restatement = (
        f"I'll look for {', '.join(roles)} relationships"
        + (f" relevant to {entity}" if entity else "")
        + f" across {', '.join(accounts)}, focusing on the last {lookback} years."
    )

    parsed.update(
        {
            "goal_type": goal,
            "beneficiary_entity": entity,
            "target_roles": roles,
            "exclusions": exclusions,
            "lookback_years": lookback,
            "recommended_accounts": accounts,
            "assumptions": assumptions,
            "restatement": restatement,
        }
    )
    parsed["clarifying_questions"] = _clarifying_questions(parsed, round_num)
    return parsed


async def parse_objective(
    objective: str,
    *,
    round_num: int = 0,
    prior_answers: list[str] | None = None,
) -> dict[str, Any]:
    """Parse with Anthropic when configured; otherwise heuristic."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        base = parse_objective_heuristic(objective, round_num=round_num)
        if prior_answers:
            base["assumptions"] = list(base.get("assumptions") or []) + [
                f"User clarification: {a}" for a in prior_answers if a.strip()
            ]
            # After answers, drop questions
            if round_num > 0:
                base["clarifying_questions"] = _clarifying_questions(base, round_num)
        return base

    try:
        from app.services.ai_service import _call_anthropic

        system = (
            "You parse relationship-outreach objectives into JSON only. "
            "Return a single JSON object with keys: goal_type, beneficiary_entity, "
            "target_roles (array), geo, exclusions (array), lookback_years (int), "
            "priority_weights (object), recommended_accounts (subset of edge,galaxy,northwyn — "
            "never include careers unless the user explicitly asks), assumptions (array), "
            "clarifying_questions (array, max 3, empty if enough info), restatement (string). "
            "goal_type one of: fundraising, customer_intros, reconnect, find_expert, "
            "find_partners, revive, other."
        )
        user = f"Objective:\n{objective}\n\nClarification round: {round_num}\n"
        if prior_answers:
            user += "Prior answers:\n" + "\n".join(f"- {a}" for a in prior_answers)
        raw = await _call_anthropic(system, user)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        base = empty_parsed()
        base.update({k: data[k] for k in base if k in data})
        # Enforce careers never default-on
        rec = [a for a in (base.get("recommended_accounts") or []) if a != "careers"]
        if not rec:
            rec = _recommend_accounts(objective)
        base["recommended_accounts"] = rec
        qs = list(base.get("clarifying_questions") or [])[:3]
        base["clarifying_questions"] = qs
        if base.get("goal_type") not in VALID_GOAL_TYPES:
            base["goal_type"] = _infer_goal_type(objective)
        return base
    except Exception as exc:
        logger.warning("Objective LLM parse failed, using heuristic: %s", exc)
        return parse_objective_heuristic(objective, round_num=round_num)


def apply_defaults_skip(parsed: dict[str, Any]) -> dict[str, Any]:
    """User chose 'use your defaults' — clear pending questions."""
    out = dict(parsed)
    out["clarifying_questions"] = []
    assumptions = list(out.get("assumptions") or [])
    assumptions.append("User accepted defaults without further clarification")
    out["assumptions"] = assumptions
    return out
