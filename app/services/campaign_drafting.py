"""P7 — campaign drafting with provenance chips and banned-phrase lint.

Drafts lead with one mined email signal (+ optional approved public fact).
Reviewer meta (email counts, citable-message tallies) stays out of the body.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models.campaign import Campaign, CampaignCandidate, CampaignDraft, EvidenceItem, ExternalFact
from app.services.campaign_service import audit
from app.services.conversation_mining import extract_salient_sentence
from app.services.research.pipeline import facts_usable_in_drafts

logger = logging.getLogger(__name__)

# How many LLM draft calls to run at once. Anthropic tolerates modest parallelism;
# this turns N sequential network round-trips into ceil(N / _DRAFT_CONCURRENCY).
_DRAFT_CONCURRENCY = 6

BANNED_PHRASES = [
    "i researched you",
    "i saw several articles",
    "my system identified",
    "i googled you",
    "according to my research",
    "i found online that",
]

# Meta scaffolding that must never appear in recipient-facing body
META_SCAFFOLDING_RE = re.compile(
    r"(you exchanged about \d+ emails|"
    r"\d+ citable messages|"
    r"most recent cited exchange|"
    r"best cited exchange|"
    r"support this recommendation)",
    re.I,
)

VALID_VARIANTS = {"email", "call_script", "linkedin"}

ASK_BY_GOAL: dict[str, dict[str, str]] = {
    "fundraising": {
        "solid": "Would you have 15 minutes to compare notes on the current raise?",
        "needs_reconnection": "Would a short catch-up call in the next couple of weeks work?",
        "weak_relationship": "Open to a brief intro call if the timing is right?",
        "default": "Would you have 15 minutes to discuss the capital raise?",
    },
    "reconnect": {
        "solid": "Any chance of a short call in the next couple of weeks?",
        "needs_reconnection": "Would love 15 minutes to reconnect when you have a window.",
        "default": "Would you have 15 minutes soon?",
    },
    "revive": {
        "default": "Would a short catch-up be useful in the next few weeks?",
    },
    "find_expert": {
        "default": "Could I get 15 minutes to tap your expertise on this?",
    },
    "find_partners": {
        "default": "Open to 15 minutes to see whether a partnership still fits?",
    },
    "customer_intros": {
        "default": "Would you have 15 minutes to explore a possible intro?",
    },
}


def lint_banned_phrases(body: str) -> list[str]:
    lower = (body or "").lower()
    return [p for p in BANNED_PHRASES if p in lower]


def strip_meta_scaffolding(body: str) -> str:
    """Remove reviewer meta that accidentally leaked into the draft body."""
    if not body:
        return body
    lines = []
    for para in body.split("\n"):
        if META_SCAFFOLDING_RE.search(para):
            continue
        lines.append(para)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ensure_linkedin_signature(body: str, url: str | None, *, variant: str = "email") -> str:
    if variant != "email" or not url:
        return body
    if url.lower() in (body or "").lower():
        return body
    return (body or "").rstrip() + f"\n\nLinkedIn: {url}"


def _evidence_hook(evidence: list[EvidenceItem]) -> str:
    """Human hook from mined summary — not raw Bid-spam subjects."""
    if not evidence:
        return "our last exchange"
    e0 = evidence[0]
    salient = extract_salient_sentence(e0.summary) if e0.summary else None
    if salient and not _looks_like_raw_subject_hook(salient, e0.subject):
        return salient.rstrip(".")
    subj = (e0.subject or "our last exchange").strip()
    if subj.lower().startswith(("re:", "fw:", "fwd:")):
        subj = re.sub(r"^(re|fw|fwd):\s*", "", subj, flags=re.I).strip() or subj
    return subj


def _looks_like_raw_subject_hook(text: str, subject: str | None) -> bool:
    if not subject:
        return False
    return text.strip().lower() == subject.strip().lower()


def _resolve_ask(
    strategy: dict[str, Any],
    *,
    goal_type: str | None,
    strength_label: str | None,
) -> str:
    custom = (strategy.get("ask") or "").strip()
    if custom:
        return custom
    goal = (goal_type or "other").lower()
    strength = (strength_label or "default").lower()
    bucket = ASK_BY_GOAL.get(goal) or ASK_BY_GOAL.get("reconnect", {})
    return (
        bucket.get(strength)
        or bucket.get("default")
        or "Would you have 15 minutes soon?"
    )


def _fact_line(facts: list[ExternalFact]) -> tuple[str, list[str]]:
    for f in facts:
        if f.status == "approved" and (f.confidence or "").lower() != "low":
            claim = (f.claim or "").strip().rstrip(".")
            if not claim:
                continue
            return (f" Congratulations again on {claim}.", [f.id])
    return ("", [])


def _heuristic_draft(
    cand: CampaignCandidate,
    *,
    strategy: dict[str, Any],
    evidence: list[EvidenceItem],
    facts: list[ExternalFact],
    linkedin_url: str | None,
    variant: str = "email",
    goal_type: str | None = None,
) -> dict[str, Any]:
    ask = _resolve_ask(
        strategy,
        goal_type=goal_type,
        strength_label=cand.strength_label,
    )
    notes = (strategy.get("notes") or "").strip()
    first = (cand.full_name or "there").split()[0]
    hook = _evidence_hook(evidence)
    mined = bool(evidence and extract_salient_sentence(evidence[0].summary))
    if evidence and mined:
        ev_line = f"When we last traded notes — {hook}."
    elif evidence:
        ev_line = f"I've been thinking back to {hook}."
    else:
        ev_line = "I hope you're well."

    fact_line, public_ids = _fact_line(facts)

    if variant == "call_script":
        subject = f"Call script — {cand.full_name or first}"
        body = (
            f"OPENING\nHi {first}, thanks for taking the call.\n\n"
            f"CONTEXT\n{ev_line}{fact_line}\n\n"
            f"{('NOTES\n' + notes + chr(10) + chr(10)) if notes else ''}"
            f"ASK\n{ask}\n\n"
            f"CLOSE\nAppreciate your time — I'll send a short note after."
        )
    elif variant == "linkedin":
        subject = f"LinkedIn DM — {cand.full_name or first}"
        body = f"Hi {first} — {ev_line} {ask}{fact_line}".strip()
        if len(body) > 300:
            body = body[:297] + "…"
    else:
        subject = f"Reconnecting — {cand.full_name or 'hello'}"
        body = (
            f"Hi {first},\n\n"
            f"I hope you're well. {ev_line}{fact_line}\n\n"
            f"{notes + chr(10) + chr(10) if notes else ''}"
            f"{ask}\n\n"
            f"Best regards"
        )
        body = ensure_linkedin_signature(body, linkedin_url, variant=variant)

    body = strip_meta_scaffolding(body)
    warnings = lint_banned_phrases(body)
    chips = []
    for e in evidence[:4]:
        label = extract_salient_sentence(e.summary) or e.subject or "Email"
        chips.append(
            {
                "kind": "private",
                "label": (label[:80] + ("…" if len(label) > 80 else "")),
                "id": e.id,
                "message_id": e.message_id,
            }
        )
    for f in facts:
        if f.id in public_ids:
            chips.append(
                {
                    "kind": "public",
                    "label": (f.claim[:60] + "…") if f.claim and len(f.claim) > 60 else (f.claim or ""),
                    "id": f.id,
                }
            )
    provenance = {
        "evidence_ids": [e.id for e in evidence[:6]],
        "message_ids": [e.message_id for e in evidence[:6] if e.message_id],
        "fact_ids": public_ids,
        "chips": chips,
        "hook_source": "mined_summary" if mined else "subject",
    }
    return {
        "subject": subject,
        "body": body,
        "ask": ask,
        "warnings": warnings,
        "provenance": provenance,
        "variant": variant,
    }


async def _llm_draft(
    cand: CampaignCandidate,
    *,
    strategy: dict[str, Any],
    evidence: list[EvidenceItem],
    facts: list[ExternalFact],
    linkedin_url: str | None,
    variant: str = "email",
    goal_type: str | None = None,
) -> dict[str, Any] | None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    try:
        from app.services.ai_service import _call_anthropic
        import json

        ask = _resolve_ask(
            strategy,
            goal_type=goal_type,
            strength_label=cand.strength_label,
        )
        ev_blob = "\n".join(
            f"- [message_id={e.message_id}] {e.occurred_at}: {e.subject} — {e.summary}"
            for e in evidence[:6]
        )
        fact_blob = "\n".join(
            f"- [{f.id}] ({f.status}, confidence={f.confidence}) {f.claim}"
            for f in facts
            if f.status in ("approved", "background")
        )
        channel = {
            "email": "short professional outreach email",
            "call_script": "phone call script with OPENING/CONTEXT/ASK/CLOSE sections",
            "linkedin": "very short LinkedIn DM (under 300 chars)",
        }.get(variant, "short professional outreach email")
        system = (
            f"Write a {channel}. Lead with ONE mined relationship signal from the evidence "
            "summaries (real words / topics — never invent). "
            "Use at most one approved public fact with Medium/High confidence. "
            "Never mention email counts, 'citable messages', or confidence scores in the body. "
            "Never use banned surveillance phrasing "
            f"({', '.join(BANNED_PHRASES)}). "
            "Return JSON: subject, body, ask. "
        )
        if variant == "email":
            system += f"End the body with LinkedIn: {linkedin_url or '(omit if none)'}."
        user = (
            f"Recipient: {cand.full_name} <{cand.email}> at {cand.company}\n"
            f"Goal type: {goal_type or 'other'}\n"
            f"Relationship strength: {cand.strength_label}\n"
            f"Strategy notes: {strategy.get('notes')}\nAsk suggestion: {ask}\n"
            f"Evidence (use summary text, not just subject):\n{ev_blob}\n"
            f"Public facts (approved only for body; withhold Low confidence):\n{fact_blob}\n"
        )
        raw = await _call_anthropic(system, user)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        body = ensure_linkedin_signature(
            str(data.get("body") or ""), linkedin_url, variant=variant
        )
        body = strip_meta_scaffolding(body)
        warnings = lint_banned_phrases(body)
        approved_facts = [
            f
            for f in facts
            if f.status == "approved" and (f.confidence or "").lower() != "low"
        ][:1]
        provenance = {
            "evidence_ids": [e.id for e in evidence[:6]],
            "message_ids": [e.message_id for e in evidence[:6] if e.message_id],
            "fact_ids": [f.id for f in approved_facts],
            "chips": (
                [
                    {
                        "kind": "private",
                        "label": (extract_salient_sentence(e.summary) or e.subject or "Email")[:80],
                        "id": e.id,
                        "message_id": e.message_id,
                    }
                    for e in evidence[:4]
                ]
                + [
                    {"kind": "public", "label": (f.claim or "")[:60], "id": f.id}
                    for f in approved_facts
                ]
            ),
            "hook_source": "llm_mined",
        }
        return {
            "subject": data.get("subject") or f"Reconnecting — {cand.full_name}",
            "body": body,
            "ask": data.get("ask") or ask,
            "warnings": warnings,
            "provenance": provenance,
            "variant": variant,
        }
    except Exception as exc:
        logger.warning("LLM draft failed: %s", exc)
        return None


def assert_provenance_facts_allowed(
    db: Session, campaign_id: str, provenance: dict[str, Any]
) -> None:
    allowed = {f.id for f in facts_usable_in_drafts(db, campaign_id)}
    for fid in provenance.get("fact_ids") or []:
        if fid not in allowed:
            raise ValueError(f"Unapproved external fact cannot appear in draft provenance: {fid}")


def _campaign_goal_type(campaign: Campaign) -> str | None:
    parsed = campaign.objective_parsed or {}
    if isinstance(parsed, dict) and parsed.get("goal_type"):
        return str(parsed["goal_type"])
    return None


async def _compose_draft_data(
    cand: CampaignCandidate,
    *,
    strategy: dict[str, Any],
    evidence: list[EvidenceItem],
    facts: list[ExternalFact],
    linkedin_url: str | None,
    variant: str,
    goal_type: str | None,
) -> dict[str, Any]:
    """Slow part only (LLM network / heuristic). No DB access — safe to run concurrently."""
    draft_data = await _llm_draft(
        cand,
        strategy=strategy,
        evidence=evidence,
        facts=facts,
        linkedin_url=linkedin_url,
        variant=variant,
        goal_type=goal_type,
    )
    if not draft_data:
        draft_data = _heuristic_draft(
            cand,
            strategy=strategy,
            evidence=evidence,
            facts=facts,
            linkedin_url=linkedin_url,
            variant=variant,
            goal_type=goal_type,
        )
    return draft_data


def _draft_row_from_data(
    campaign_id: str, cand_id: str, draft_data: dict[str, Any], *, variant: str
) -> CampaignDraft:
    return CampaignDraft(
        campaign_id=campaign_id,
        candidate_id=cand_id,
        subject=draft_data["subject"],
        body=draft_data["body"],
        status="generated",
        lifecycle="approved_pending",
        provenance=draft_data["provenance"],
        ask=draft_data.get("ask"),
        warnings=draft_data.get("warnings") or [],
        variant=variant,
    )


async def _build_one_draft(
    db: Session,
    campaign: Campaign,
    cand: CampaignCandidate,
    *,
    variant: str,
    facts: list[ExternalFact],
) -> CampaignDraft:
    strategy = dict(campaign.message_strategy or {})
    linkedin = get_settings().linkedin_signature_url or None
    evidence = list(cand.evidence_items or [])
    goal_type = _campaign_goal_type(campaign)
    draft_data = await _compose_draft_data(
        cand,
        strategy=strategy,
        evidence=evidence,
        facts=facts,
        linkedin_url=linkedin,
        variant=variant,
        goal_type=goal_type,
    )
    assert_provenance_facts_allowed(db, campaign.id, draft_data["provenance"])
    return _draft_row_from_data(campaign.id, cand.id, draft_data, variant=variant)


async def generate_campaign_drafts(
    db: Session,
    campaign_id: str,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[CampaignDraft]:
    """Generate one draft per included candidate.

    LLM calls run concurrently (bounded by _DRAFT_CONCURRENCY) so total wall time is
    roughly ceil(N / concurrency) round-trips instead of N. ``progress_cb(done, total)``
    is invoked from the event loop after each draft is persisted.
    """
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("Campaign not found")
    usable_facts = facts_usable_in_drafts(db, campaign_id)
    facts_by_cand: dict[str, list[ExternalFact]] = {}
    for f in usable_facts:
        facts_by_cand.setdefault(f.candidate_id, []).append(f)
    # Compute allowed fact ids once instead of a DB query per candidate.
    allowed_fact_ids = {f.id for f in usable_facts}

    db.query(CampaignDraft).filter(CampaignDraft.campaign_id == campaign_id).delete()
    db.flush()

    included = (
        db.query(CampaignCandidate)
        .options(joinedload(CampaignCandidate.evidence_items))
        .filter(
            CampaignCandidate.campaign_id == campaign_id,
            CampaignCandidate.decision == "include",
        )
        .order_by(CampaignCandidate.rank.asc())
        .all()
    )
    total = len(included)

    strategy = dict(campaign.message_strategy or {})
    linkedin = get_settings().linkedin_signature_url or None
    goal_type = _campaign_goal_type(campaign)
    cand_by_id = {c.id: c for c in included}

    sem = asyncio.Semaphore(_DRAFT_CONCURRENCY)

    async def _compose(cand: CampaignCandidate) -> tuple[str, dict[str, Any]]:
        async with sem:
            evidence = list(cand.evidence_items or [])
            data = await _compose_draft_data(
                cand,
                strategy=strategy,
                evidence=evidence,
                facts=facts_by_cand.get(cand.id, []),
                linkedin_url=linkedin,
                variant="email",
                goal_type=goal_type,
            )
            return cand.id, data

    tasks = [asyncio.create_task(_compose(c)) for c in included]

    created: list[CampaignDraft] = []
    done = 0
    if progress_cb:
        progress_cb(0, total)
    # Persist in completion order; DB writes happen here on the event loop (single
    # threaded), so the shared Session is only ever touched by one coroutine at a time.
    for coro in asyncio.as_completed(tasks):
        cand_id, draft_data = await coro
        for fid in draft_data["provenance"].get("fact_ids") or []:
            if fid not in allowed_fact_ids:
                raise ValueError(
                    f"Unapproved external fact cannot appear in draft provenance: {fid}"
                )
        row = _draft_row_from_data(campaign_id, cand_id, draft_data, variant="email")
        db.add(row)
        created.append(row)
        cand = cand_by_id.get(cand_id)
        if cand:
            cand.tracking_status = "drafted"
        done += 1
        if progress_cb:
            progress_cb(done, total)

    campaign.status = "reviewing_drafts"
    audit(db, campaign_id, "drafts_generated", f"Generated {len(created)} draft(s)", {})
    db.commit()
    for row in created:
        db.refresh(row)
    return created


async def regenerate_draft(
    db: Session, campaign_id: str, draft_id: str, *, variant: str | None = None
) -> CampaignDraft:
    campaign = db.get(Campaign, campaign_id)
    draft = db.get(CampaignDraft, draft_id)
    if not campaign or not draft or draft.campaign_id != campaign_id:
        raise ValueError("Draft not found")
    cand = (
        db.query(CampaignCandidate)
        .options(joinedload(CampaignCandidate.evidence_items))
        .filter(CampaignCandidate.id == draft.candidate_id)
        .first()
    )
    if not cand:
        raise ValueError("Candidate not found")
    v = variant or draft.variant or "email"
    if v not in VALID_VARIANTS:
        raise ValueError(f"Invalid variant: {v}")
    facts = [f for f in facts_usable_in_drafts(db, campaign_id) if f.candidate_id == cand.id]
    new_row = await _build_one_draft(db, campaign, cand, variant=v, facts=facts)
    draft.subject = new_row.subject
    draft.body = new_row.body
    draft.ask = new_row.ask
    draft.warnings = new_row.warnings
    draft.provenance = new_row.provenance
    draft.variant = v
    draft.status = "generated"
    draft.updated_at = datetime.utcnow()
    audit(db, campaign_id, "draft_regenerated", f"Regenerated draft {draft_id} as {v}", {})
    db.commit()
    db.refresh(draft)
    return draft


async def set_draft_variant(
    db: Session, campaign_id: str, draft_id: str, variant: str
) -> CampaignDraft:
    if variant not in VALID_VARIANTS:
        raise ValueError(f"Invalid variant: {variant}")
    return await regenerate_draft(db, campaign_id, draft_id, variant=variant)


def change_ask(db: Session, campaign_id: str, draft_id: str, ask: str) -> CampaignDraft:
    draft = db.get(CampaignDraft, draft_id)
    if not draft or draft.campaign_id != campaign_id:
        raise ValueError("Draft not found")
    ask = (ask or "").strip()
    if not ask:
        raise ValueError("Ask cannot be empty")
    old_ask = draft.ask or ""
    body = draft.body or ""
    if old_ask and old_ask in body:
        body = body.replace(old_ask, ask, 1)
    else:
        body = re.sub(r"\n\nLinkedIn:.*$", "", body, flags=re.I | re.S)
        body = body.rstrip() + f"\n\n{ask}"
        linkedin = get_settings().linkedin_signature_url or None
        body = ensure_linkedin_signature(body, linkedin, variant=draft.variant or "email")
    draft.ask = ask
    draft.body = strip_meta_scaffolding(body)
    draft.status = "edited"
    draft.warnings = lint_banned_phrases(draft.body)
    draft.updated_at = datetime.utcnow()
    campaign = db.get(Campaign, campaign_id)
    if campaign:
        strategy = dict(campaign.message_strategy or {})
        strategy["ask"] = ask
        campaign.message_strategy = strategy
    audit(db, campaign_id, "draft_change_ask", f"Changed ask on {draft_id}", {"ask": ask})
    db.commit()
    db.refresh(draft)
    return draft


def remove_public_refs(db: Session, campaign_id: str, draft_id: str) -> CampaignDraft:
    draft = db.get(CampaignDraft, draft_id)
    if not draft or draft.campaign_id != campaign_id:
        raise ValueError("Draft not found")
    prov = dict(draft.provenance or {})
    fact_ids = list(prov.get("fact_ids") or [])
    chips = [c for c in (prov.get("chips") or []) if c.get("kind") != "public"]
    body = draft.body or ""
    body = re.sub(
        r"\s*Congrats(?:ulations)? again on[^.]*\.",
        "",
        body,
        flags=re.I,
    )
    body = re.sub(
        r"\s*Congrats on the recent development[^.]*\.",
        "",
        body,
        flags=re.I,
    )
    for fid in fact_ids:
        body = body.replace(fid, "")
    prov["fact_ids"] = []
    prov["chips"] = chips
    draft.provenance = prov
    draft.body = strip_meta_scaffolding(body.strip())
    draft.status = "edited"
    draft.warnings = lint_banned_phrases(draft.body)
    draft.updated_at = datetime.utcnow()
    audit(db, campaign_id, "draft_remove_public", f"Removed public refs from {draft_id}", {})
    db.commit()
    db.refresh(draft)
    return draft


def apply_tone_to_body(body: str, mode: str) -> str:
    if mode == "shorter":
        parts = [p for p in body.split("\n\n") if p.strip()]
        if len(parts) > 3:
            parts = [parts[0], parts[-2], parts[-1]]
        return "\n\n".join(parts)
    if mode == "warmer":
        return body.replace("I'd value", "I'd genuinely value").replace(
            "Hope you're well", "I hope you're doing well"
        )
    if mode == "direct":
        return re.sub(
            r"Would you have 15 minutes[^.?!]*[.?!]",
            "Can we lock 15 minutes this week?",
            body,
            flags=re.I,
        )
    if mode == "formal":
        body = body.replace("Hey ", "Hello ")
        body = body.replace("Hi ", "Hello ")
        body = re.sub(r"\bI'd love to\b", "I would appreciate the opportunity to", body, flags=re.I)
        body = re.sub(r"\bThanks!\b", "Thank you.", body, flags=re.I)
        return body
    return body


def apply_tone(
    db: Session,
    campaign_id: str,
    *,
    mode: str,
    scope: str = "all",
    draft_id: str | None = None,
) -> list[CampaignDraft]:
    q = db.query(CampaignDraft).filter(CampaignDraft.campaign_id == campaign_id)
    if scope == "one" and draft_id:
        q = q.filter(CampaignDraft.id == draft_id)
    rows = q.all()
    for row in rows:
        row.body = strip_meta_scaffolding(apply_tone_to_body(row.body, mode))
        row.status = "edited"
        row.warnings = lint_banned_phrases(row.body)
        row.updated_at = datetime.utcnow()
    audit(db, campaign_id, "draft_tone", f"Applied tone={mode} scope={scope}", {"mode": mode})
    db.commit()
    return rows


def draft_to_dict(d: CampaignDraft) -> dict[str, Any]:
    return {
        "id": d.id,
        "campaign_id": d.campaign_id,
        "candidate_id": d.candidate_id,
        "subject": d.subject,
        "body": d.body,
        "status": d.status,
        "lifecycle": d.lifecycle,
        "provenance": d.provenance or {},
        "ask": d.ask,
        "warnings": d.warnings or [],
        "variant": d.variant,
        "mailbox_draft_id": d.mailbox_draft_id,
        "mailbox_draft_web_link": d.mailbox_draft_web_link,
        "sending_account_override": d.sending_account_override,
    }


def body_hash(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()
