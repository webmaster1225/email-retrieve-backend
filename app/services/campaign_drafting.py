"""P7 — campaign drafting with provenance chips and banned-phrase lint."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models.campaign import Campaign, CampaignCandidate, CampaignDraft, EvidenceItem, ExternalFact
from app.services.campaign_service import audit
from app.services.research.pipeline import facts_usable_in_drafts

logger = logging.getLogger(__name__)

BANNED_PHRASES = [
    "i researched you",
    "i saw several articles",
    "my system identified",
    "i googled you",
    "according to my research",
    "i found online that",
]

VALID_VARIANTS = {"email", "call_script", "linkedin"}


def lint_banned_phrases(body: str) -> list[str]:
    lower = (body or "").lower()
    return [p for p in BANNED_PHRASES if p in lower]


def ensure_linkedin_signature(body: str, url: str | None, *, variant: str = "email") -> str:
    if variant != "email" or not url:
        return body
    if url.lower() in (body or "").lower():
        return body
    return (body or "").rstrip() + f"\n\nLinkedIn: {url}"


def _heuristic_draft(
    cand: CampaignCandidate,
    *,
    strategy: dict[str, Any],
    evidence: list[EvidenceItem],
    facts: list[ExternalFact],
    linkedin_url: str | None,
    variant: str = "email",
) -> dict[str, Any]:
    ask = (strategy.get("ask") or "Would you have 15 minutes to reconnect?").strip()
    notes = (strategy.get("notes") or "").strip()
    why = cand.why_text or "our past correspondence"
    first = (cand.full_name or "there").split()[0]
    ev_line = ""
    if evidence:
        e0 = evidence[0]
        ev_line = f"I still remember {e0.subject or 'our last exchange'}"
        if e0.occurred_at:
            ev_line += f" ({e0.occurred_at.strftime('%b %Y')})"
        ev_line += "."
    fact_line = ""
    public_ids: list[str] = []
    for f in facts:
        if f.status == "approved":
            fact_line = f" Congrats on the recent development — {f.claim[:120]}"
            public_ids.append(f.id)
            break

    if variant == "call_script":
        subject = f"Call script — {cand.full_name or first}"
        body = (
            f"OPENING\nHi {first}, thanks for taking the call.\n\n"
            f"CONTEXT\n{ev_line} {why}\n\n"
            f"{('NOTES\n' + notes + chr(10) + chr(10)) if notes else ''}"
            f"ASK\n{ask}{fact_line}\n\n"
            f"CLOSE\nAppreciate your time — I'll send a short note after."
        )
    elif variant == "linkedin":
        subject = f"LinkedIn DM — {cand.full_name or first}"
        body = (
            f"Hi {first} — {ev_line or 'good to reconnect.'} {ask}{fact_line}"
        ).strip()
        if len(body) > 300:
            body = body[:297] + "…"
    else:
        subject = f"Reconnecting — {cand.full_name or 'hello'}"
        body = (
            f"Hi {first},\n\n"
            f"I hope you're well. {ev_line} {why}\n\n"
            f"{notes + chr(10) + chr(10) if notes else ''}"
            f"{ask}{fact_line}\n\n"
            f"Best regards"
        )
        body = ensure_linkedin_signature(body, linkedin_url, variant=variant)

    warnings = lint_banned_phrases(body)
    provenance = {
        "evidence_ids": [e.id for e in evidence[:6]],
        "fact_ids": public_ids,
        "chips": (
            [{"kind": "private", "label": e.subject or "Email", "id": e.id} for e in evidence[:4]]
            + [
                {"kind": "public", "label": (f.claim[:60] + "…"), "id": f.id}
                for f in facts
                if f.id in public_ids
            ]
        ),
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
) -> dict[str, Any] | None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        return None
    try:
        from app.services.ai_service import _call_anthropic
        import json

        ev_blob = "\n".join(
            f"- [{e.id}] {e.occurred_at}: {e.subject} — {e.summary}" for e in evidence[:6]
        )
        fact_blob = "\n".join(
            f"- [{f.id}] ({f.status}) {f.claim}"
            for f in facts
            if f.status in ("approved", "background")
        )
        channel = {
            "email": "short professional outreach email",
            "call_script": "phone call script with OPENING/CONTEXT/ASK/CLOSE sections",
            "linkedin": "very short LinkedIn DM (under 300 chars)",
        }.get(variant, "short professional outreach email")
        system = (
            f"Write a {channel}. Lead with relationship history. "
            "Use at most one approved public fact. Never use banned surveillance phrasing "
            f"({', '.join(BANNED_PHRASES)}). "
            "Return JSON: subject, body, ask. "
        )
        if variant == "email":
            system += f"End the body with LinkedIn: {linkedin_url or '(omit if none)'}."
        user = (
            f"Recipient: {cand.full_name} <{cand.email}> at {cand.company}\n"
            f"Strategy notes: {strategy.get('notes')}\nAsk: {strategy.get('ask')}\n"
            f"Evidence:\n{ev_blob}\nPublic facts (approved only for body):\n{fact_blob}\n"
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
        warnings = lint_banned_phrases(body)
        approved_facts = [f for f in facts if f.status == "approved"]
        provenance = {
            "evidence_ids": [e.id for e in evidence[:6]],
            "fact_ids": [f.id for f in approved_facts[:1]],
            "chips": (
                [{"kind": "private", "label": e.subject or "Email", "id": e.id} for e in evidence[:4]]
                + [
                    {"kind": "public", "label": f.claim[:60], "id": f.id}
                    for f in approved_facts[:1]
                ]
            ),
        }
        return {
            "subject": data.get("subject") or f"Reconnecting — {cand.full_name}",
            "body": body,
            "ask": data.get("ask") or strategy.get("ask"),
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
    draft_data = await _llm_draft(
        cand,
        strategy=strategy,
        evidence=evidence,
        facts=facts,
        linkedin_url=linkedin,
        variant=variant,
    )
    if not draft_data:
        draft_data = _heuristic_draft(
            cand,
            strategy=strategy,
            evidence=evidence,
            facts=facts,
            linkedin_url=linkedin,
            variant=variant,
        )
    assert_provenance_facts_allowed(db, campaign.id, draft_data["provenance"])
    return CampaignDraft(
        campaign_id=campaign.id,
        candidate_id=cand.id,
        subject=draft_data["subject"],
        body=draft_data["body"],
        status="generated",
        lifecycle="approved_pending",
        provenance=draft_data["provenance"],
        ask=draft_data.get("ask"),
        warnings=draft_data.get("warnings") or [],
        variant=variant,
    )


async def generate_campaign_drafts(db: Session, campaign_id: str) -> list[CampaignDraft]:
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("Campaign not found")
    usable_facts = facts_usable_in_drafts(db, campaign_id)
    facts_by_cand: dict[str, list[ExternalFact]] = {}
    for f in usable_facts:
        facts_by_cand.setdefault(f.candidate_id, []).append(f)

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
    created: list[CampaignDraft] = []
    for cand in included:
        row = await _build_one_draft(
            db, campaign, cand, variant="email", facts=facts_by_cand.get(cand.id, [])
        )
        db.add(row)
        created.append(row)
        cand.tracking_status = "drafted"
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
    draft.body = body
    draft.status = "edited"
    draft.warnings = lint_banned_phrases(body)
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
    draft.body = body.strip()
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
        row.body = apply_tone_to_body(row.body, mode)
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
