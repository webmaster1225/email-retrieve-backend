"""Outreach intelligence: history analysis, scoring, personalized drafts, batch jobs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models.outreach import OutreachJob
from app.services.ai_service import (
    AIServiceError,
    _call_anthropic,
    _ensure_context_row,
    _get_contact,
)
from app.services.exchange_service import build_exchange_prompt_context, gather_exchange_data
from app.services.scorer import score_to_tier

MAX_BATCH_CONTACTS = 50

USE_CASE_LABELS = {
    "business_development": "business development",
    "fundraising": "fundraising",
    "investment": "investment",
    "ma": "M&A",
    "board_opportunities": "board opportunities",
    "strategic_introductions": "strategic introductions",
}

PATTERN_SCORES = {
    "two_way": 25,
    "mostly_inbound": 18,
    "mostly_outbound": 8,
    "one_off": 3,
    "unknown": 0,
}

DEPTH_SCORES = {
    "deep": 20,
    "moderate": 12,
    "shallow": 5,
    "minimal": 2,
    "unknown": 0,
}

STRENGTH_SCORES = {
    "strong": 12,
    "moderate": 7,
    "weak": 3,
    "none": 0,
    "unknown": 0,
}

SENIORITY_SCORES = {
    "c_suite": 22,
    "partner": 20,
    "executive": 16,
    "director": 10,
    "manager": 5,
    "individual_contributor": 2,
    "unknown": 0,
}

USEFULNESS_SCORES = {
    "high": 25,
    "medium": 12,
    "low": 4,
    "none": 0,
}

ANALYSIS_SYSTEM = (
    "You analyze email conversation history for Edge Investing / Galaxy Pharma "
    "relationship intelligence and outreach prioritization. Be evidence-based. "
    "Never invent facts not supported by the email data."
)

ANALYSIS_USER_TEMPLATE = """Analyze this contact's email history for outreach prioritization.

Reply in this exact JSON format only (no markdown):
{{
  "conversation_pattern": "two_way|mostly_outbound|mostly_inbound|one_off|unknown",
  "relationship_depth": "deep|moderate|shallow|minimal|unknown",
  "relationship_strength": "strong|moderate|weak|none|unknown",
  "seniority": {{
    "title": "best guess title or null",
    "level": "c_suite|partner|executive|director|manager|individual_contributor|unknown",
    "is_senior": true
  }},
  "use_case_relevance": {{
    "business_development": "high|medium|low|none",
    "fundraising": "high|medium|low|none",
    "investment": "high|medium|low|none",
    "ma": "high|medium|low|none",
    "board_opportunities": "high|medium|low|none",
    "strategic_introductions": "high|medium|low|none"
  }},
  "primary_use_case": "business_development|fundraising|investment|ma|board_opportunities|strategic_introductions|limited|unknown",
  "last_discussed_topic": "specific subject/theme from prior conversation, or null if none",
  "key_conversation_points": ["short bullet of what was discussed", "another point if available"],
  "personalization_hook": "one sentence opener referencing real prior context, or null",
  "summary": "2-3 sentences on relationship quality and opportunity",
  "confidence": "high|medium|low",
  "evidence": "one sentence citing concrete email evidence"
}}

Guidance:
- two_way = genuine back-and-forth; mostly_outbound = we emailed them with little/no reply
- depth reflects substance of topics, not just message count
- Seniority examples: CEO, Founder, Managing Director, Partner, Executive, VP
- Use-case relevance must reflect email content and role signals
- last_discussed_topic and personalization_hook must come from actual subjects/previews
- If history is thin, use low confidence and unknown/minimal/none where appropriate
{target_block}
Email history:
{context}"""

PERSONALIZED_DRAFT_SYSTEM = (
    "You write professional, warm, personalized outreach emails for Edge Investing / Galaxy Pharma. "
    "You MUST ground the email in the contact's actual prior conversation history. "
    "Never invent facts, meetings, or topics not supported by the analysis or email data."
)

PERSONALIZED_DRAFT_TEMPLATE = """Draft a personalized outreach email to reconnect with this contact.

Critical requirements:
- Open by referencing a SPECIFIC prior conversation topic from the analysis (e.g. "Last time we spoke, we discussed …")
- Then connect to the current purpose / opportunity
- Use the personalization_hook and last_discussed_topic when available
- Professional, warm tone; clear call to action
- Do not invent facts not in the data
- Under 200 words for the body
- Sign off as "Best regards" without a name

{custom_instructions_block}
{target_block}

Return ONLY in this format:
Subject: <subject line>

<body paragraphs>

Structured relationship analysis:
{analysis_json}

Email exchange context:
{context}"""


class OutreachIntelligenceError(Exception):
    pass


def _parse_json_object(raw: str) -> dict:
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        return json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


def _normalize_analysis(raw: dict) -> dict:
    seniority = raw.get("seniority") if isinstance(raw.get("seniority"), dict) else {}
    use_cases = raw.get("use_case_relevance") if isinstance(raw.get("use_case_relevance"), dict) else {}
    points = raw.get("key_conversation_points")
    if not isinstance(points, list):
        points = []

    return {
        "conversation_pattern": raw.get("conversation_pattern") or "unknown",
        "relationship_depth": raw.get("relationship_depth") or "unknown",
        "relationship_strength": raw.get("relationship_strength") or "unknown",
        "seniority": {
            "title": seniority.get("title"),
            "level": seniority.get("level") or "unknown",
            "is_senior": bool(seniority.get("is_senior")),
        },
        "use_case_relevance": {
            key: use_cases.get(key) or "none"
            for key in (
                "business_development",
                "fundraising",
                "investment",
                "ma",
                "board_opportunities",
                "strategic_introductions",
            )
        },
        "primary_use_case": raw.get("primary_use_case") or "unknown",
        "last_discussed_topic": raw.get("last_discussed_topic"),
        "key_conversation_points": [str(p) for p in points[:6]],
        "personalization_hook": raw.get("personalization_hook"),
        "summary": raw.get("summary") or "",
        "confidence": raw.get("confidence") or "low",
        "evidence": raw.get("evidence") or raw.get("reason") or "",
    }


def compute_outreach_score(
    analysis: dict,
    stats: dict,
    *,
    target_use_case: str | None = None,
) -> tuple[int, dict[str, int], str]:
    """Deterministic score from LLM analysis + exchange stats. Returns score, breakdown, explanation."""
    breakdown: dict[str, int] = {}

    pattern = analysis.get("conversation_pattern") or "unknown"
    breakdown["conversation_pattern"] = PATTERN_SCORES.get(pattern, 0)

    depth = analysis.get("relationship_depth") or "unknown"
    breakdown["relationship_depth"] = DEPTH_SCORES.get(depth, 0)

    strength = analysis.get("relationship_strength") or "unknown"
    breakdown["relationship_strength"] = STRENGTH_SCORES.get(strength, 0)

    seniority = analysis.get("seniority") or {}
    level = seniority.get("level") or "unknown"
    breakdown["seniority"] = SENIORITY_SCORES.get(level, 0)
    if seniority.get("is_senior") and breakdown["seniority"] < 16:
        breakdown["seniority"] = max(breakdown["seniority"], 16)

    use_cases = analysis.get("use_case_relevance") or {}
    primary = target_use_case or analysis.get("primary_use_case") or "unknown"
    if primary in USE_CASE_LABELS:
        usefulness = use_cases.get(primary) or "none"
        breakdown["primary_use_case"] = USEFULNESS_SCORES.get(usefulness, 0)
    else:
        # Fall back to best use-case rating
        best = max((USEFULNESS_SCORES.get(v, 0) for v in use_cases.values()), default=0)
        breakdown["primary_use_case"] = best

    # Small boost from hard exchange stats (ground truth)
    if stats.get("has_two_way"):
        breakdown["two_way_bonus"] = 5
    elif stats.get("inbound_count", 0) == 0 and stats.get("outbound_count", 0) > 0:
        breakdown["one_way_penalty"] = -5

    msg_count = int(stats.get("total_count") or 0)
    if msg_count >= 10:
        breakdown["volume"] = 5
    elif msg_count >= 4:
        breakdown["volume"] = 3
    elif msg_count >= 1:
        breakdown["volume"] = 1

    score = max(0, min(100, sum(breakdown.values())))

    parts: list[str] = []
    if pattern == "two_way":
        parts.append("genuine two-way conversation history")
    elif pattern == "mostly_outbound":
        parts.append("mostly one-way outbound outreach with limited replies")
    elif pattern == "mostly_inbound":
        parts.append("they have initiated meaningful inbound messages")
    elif pattern == "one_off":
        parts.append("only a one-off exchange")

    if depth in ("deep", "moderate"):
        parts.append(f"{depth} relationship depth")
    if strength in ("strong", "moderate"):
        parts.append(f"{strength} relationship strength")

    title = seniority.get("title")
    if title:
        parts.append(f"seniority signal: {title}")
    elif seniority.get("is_senior"):
        parts.append(f"senior decision-maker ({level})")

    primary_label = USE_CASE_LABELS.get(primary, primary.replace("_", " ") if primary else "general")
    usefulness = use_cases.get(primary) if primary in use_cases else None
    if usefulness and usefulness != "none":
        parts.append(f"{usefulness} relevance for {primary_label}")

    topic = analysis.get("last_discussed_topic")
    if topic:
        parts.append(f"prior topic: {topic}")

    evidence = analysis.get("evidence") or analysis.get("summary") or ""
    body = "; ".join(parts)
    if body:
        body = body[0].upper() + body[1:] + ". "
    explanation = (
        f"Score {score}/100 ({score_to_tier(score)}). "
        + body
        + (evidence if evidence else "Limited email evidence available.")
    )
    return score, breakdown, explanation.strip()


def _intelligence_needs_refresh(payload: dict | None, contact) -> bool:
    if not payload or not payload.get("analysis"):
        return True
    generated_raw = payload.get("generated_at")
    if not generated_raw or not contact.last_contacted_at:
        return False
    try:
        generated_at = datetime.fromisoformat(str(generated_raw))
    except ValueError:
        return True
    return contact.last_contacted_at > generated_at


def _target_block(target_use_case: str | None) -> str:
    if not target_use_case or target_use_case not in USE_CASE_LABELS:
        return ""
    label = USE_CASE_LABELS[target_use_case]
    return f"\nPrioritize scoring and drafting for this use case: {label}.\n"


def analysis_to_response(payload: dict, *, cached: bool = False) -> dict:
    return {
        "contact_id": payload.get("contact_id"),
        "stats": payload.get("stats") or {},
        "analysis": payload.get("analysis") or {},
        "score": payload.get("score", 0),
        "tier": payload.get("tier"),
        "score_breakdown": payload.get("score_breakdown") or {},
        "score_explanation": payload.get("score_explanation") or "",
        "generated_at": payload.get("generated_at"),
        "cached": cached,
        "draft_id": payload.get("draft_id"),
    }


async def analyze_contact_intelligence(
    db: Session,
    contact_id: str,
    *,
    force: bool = False,
    target_use_case: str | None = None,
) -> dict:
    contact = _get_contact(db, contact_id)
    ctx = _ensure_context_row(db, contact)

    existing = ctx.ai_outreach_intelligence
    if (
        not force
        and existing
        and not _intelligence_needs_refresh(existing, contact)
        and (not target_use_case or existing.get("target_use_case") == target_use_case)
    ):
        payload = {**existing, "contact_id": contact_id}
        return analysis_to_response(payload, cached=True)

    try:
        stats, messages = await gather_exchange_data(
            db,
            contact.primary_email,
            full_name=contact.full_name,
            company_name=contact.company_name,
        )
    except Exception as exc:
        raise OutreachIntelligenceError(f"Failed to load email history: {exc}") from exc

    prompt_context = build_exchange_prompt_context(
        contact_email=contact.primary_email,
        full_name=contact.full_name,
        company_name=contact.company_name,
        stats=stats,
        messages=messages,
    )

    user_prompt = ANALYSIS_USER_TEMPLATE.format(
        context=prompt_context,
        target_block=_target_block(target_use_case),
    )
    try:
        raw = await _call_anthropic(ANALYSIS_SYSTEM, user_prompt)
    except AIServiceError as exc:
        raise OutreachIntelligenceError(str(exc)) from exc

    analysis = _normalize_analysis(_parse_json_object(raw))
    score, breakdown, explanation = compute_outreach_score(
        analysis, stats, target_use_case=target_use_case
    )
    tier = score_to_tier(score)
    generated_at = datetime.utcnow().isoformat()

    payload = {
        "contact_id": contact_id,
        "generated_at": generated_at,
        "target_use_case": target_use_case,
        "stats": stats,
        "analysis": analysis,
        "score": score,
        "tier": tier,
        "score_breakdown": breakdown,
        "score_explanation": explanation,
        "model": get_settings().anthropic_model,
    }

    ctx.ai_outreach_intelligence = payload
    # Keep related caches in sync for the Contacts drawer
    ctx.ai_relationship_analysis = {
        "generated_at": generated_at,
        "stats": stats,
        "analysis": {
            "conversation_pattern": analysis["conversation_pattern"],
            "conversation_depth": analysis["relationship_depth"],
            "depth_score": DEPTH_SCORES.get(analysis["relationship_depth"], 1),
            "business_usefulness": analysis["use_case_relevance"],
            "primary_value": analysis["primary_use_case"],
            "summary": analysis["summary"],
            "confidence": analysis["confidence"],
            "reason": analysis["evidence"],
        },
    }
    ctx.ai_seniority = {
        "title": analysis["seniority"].get("title"),
        "seniority_level": analysis["seniority"].get("level"),
        "is_senior": analysis["seniority"].get("is_senior"),
        "confidence": analysis["confidence"],
        "reason": analysis["evidence"],
    }
    if analysis["seniority"].get("title"):
        ctx.detected_role = analysis["seniority"]["title"]
    ctx.ai_summary_generated_at = datetime.utcnow()
    ctx.ai_model_used = get_settings().anthropic_model + "-outreach-intel"
    ctx.updated_at = datetime.utcnow()

    contact.outreach_relevance_score = score
    contact.outreach_relevance_tier = tier
    contact.outreach_score_explanation = explanation
    contact.updated_at = datetime.utcnow()

    db.commit()
    return analysis_to_response(payload, cached=False)


def get_contact_intelligence(db: Session, contact_id: str) -> dict | None:
    contact = _get_contact(db, contact_id)
    ctx = contact.context
    if not ctx or not ctx.ai_outreach_intelligence:
        return None
    payload = {**ctx.ai_outreach_intelligence, "contact_id": contact_id}
    return analysis_to_response(payload, cached=True)


def build_personalized_context(contact, payload: dict) -> str:
    analysis = payload.get("analysis") or {}
    stats = payload.get("stats") or {}
    lines = [
        f"Contact: {contact.full_name} <{contact.primary_email}>",
        f"Company: {contact.company_name or 'Unknown'}",
        f"Outreach relevance score: {payload.get('score')} ({payload.get('tier')})",
        f"Score explanation: {payload.get('score_explanation')}",
        "",
        f"Conversation pattern: {analysis.get('conversation_pattern')}",
        f"Relationship depth: {analysis.get('relationship_depth')}",
        f"Relationship strength: {analysis.get('relationship_strength')}",
        f"Seniority: {analysis.get('seniority')}",
        f"Primary use case: {analysis.get('primary_use_case')}",
        f"Use-case relevance: {analysis.get('use_case_relevance')}",
        f"Last discussed topic: {analysis.get('last_discussed_topic')}",
        f"Personalization hook: {analysis.get('personalization_hook')}",
        f"Key points: {analysis.get('key_conversation_points')}",
        f"Summary: {analysis.get('summary')}",
        "",
        "Exchange stats:",
        f"- Outbound: {stats.get('outbound_count', 0)}, Inbound: {stats.get('inbound_count', 0)}",
        f"- Two-way: {stats.get('has_two_way')}, Threads: {stats.get('thread_count')}",
    ]
    return "\n".join(lines)


async def generate_personalized_draft(
    db: Session,
    contact_id: str,
    *,
    custom_instructions: str | None = None,
    target_use_case: str | None = None,
    force_analyze: bool = False,
):
    from app.models.outreach import EmailDraft
    from app.services.outreach_service import _parse_draft_response

    contact = _get_contact(db, contact_id)
    if contact.review_status != "approved":
        raise OutreachIntelligenceError(f"Contact {contact.primary_email} is not approved for outreach")

    intel = await analyze_contact_intelligence(
        db,
        contact_id,
        force=force_analyze,
        target_use_case=target_use_case,
    )
    db.refresh(contact)
    ctx = contact.context
    payload = (ctx.ai_outreach_intelligence if ctx else None) or {}
    analysis = payload.get("analysis") or intel.get("analysis") or {}

    stats, messages = await gather_exchange_data(
        db,
        contact.primary_email,
        full_name=contact.full_name,
        company_name=contact.company_name,
    )
    exchange_context = build_exchange_prompt_context(
        contact_email=contact.primary_email,
        full_name=contact.full_name,
        company_name=contact.company_name,
        stats=stats,
        messages=messages,
    )

    custom_block = ""
    if custom_instructions and custom_instructions.strip():
        custom_block = f"Additional instructions from the user:\n{custom_instructions.strip()}\n"

    analysis_json = json.dumps(analysis, indent=2)
    user_prompt = PERSONALIZED_DRAFT_TEMPLATE.format(
        custom_instructions_block=custom_block,
        target_block=_target_block(target_use_case),
        analysis_json=analysis_json,
        context=exchange_context + "\n\n" + build_personalized_context(contact, payload),
    )
    system_prompt = PERSONALIZED_DRAFT_SYSTEM

    try:
        raw = await _call_anthropic(system_prompt, user_prompt)
    except AIServiceError as exc:
        raise OutreachIntelligenceError(str(exc)) from exc

    subject, body = _parse_draft_response(raw)

    existing = (
        db.query(EmailDraft)
        .filter(EmailDraft.contact_id == contact_id, EmailDraft.status.in_(["draft", "approved"]))
        .order_by(EmailDraft.created_at.desc())
        .first()
    )
    draft = existing or EmailDraft(contact_id=contact_id)
    draft.subject = subject
    draft.body = body
    draft.status = "draft"
    draft.custom_instructions = custom_instructions
    draft.system_prompt = system_prompt
    draft.user_prompt = user_prompt
    draft.error_message = None
    draft.updated_at = datetime.utcnow()
    if not existing:
        db.add(draft)
    db.commit()
    db.refresh(draft)

    if contact.context and contact.context.ai_outreach_intelligence:
        intel_payload = dict(contact.context.ai_outreach_intelligence)
        intel_payload["draft_id"] = draft.id
        contact.context.ai_outreach_intelligence = intel_payload
        db.commit()

    return draft, analysis_to_response(
        {**(contact.context.ai_outreach_intelligence if contact.context else {}), "contact_id": contact_id},
        cached=intel.get("cached", False),
    )


def job_to_dict(job: OutreachJob) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "job_type": job.job_type,
        "contact_ids": job.contact_ids or [],
        "total": job.total,
        "completed": job.completed,
        "failed": job.failed,
        "generate_drafts": job.generate_drafts,
        "custom_instructions": job.custom_instructions,
        "target_use_case": job.target_use_case,
        "force": job.force,
        "results": job.results or [],
        "error_message": job.error_message,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "updated_at": job.updated_at,
        "progress_pct": int((job.completed / job.total) * 100) if job.total else 0,
    }


def create_batch_job(
    db: Session,
    contact_ids: list[str],
    *,
    generate_drafts: bool = False,
    custom_instructions: str | None = None,
    target_use_case: str | None = None,
    force: bool = False,
) -> OutreachJob:
    unique_ids = list(dict.fromkeys(contact_ids))
    if not unique_ids:
        raise OutreachIntelligenceError("Select at least one contact")
    if len(unique_ids) > MAX_BATCH_CONTACTS:
        raise OutreachIntelligenceError(f"Batch limit is {MAX_BATCH_CONTACTS} contacts")

    job = OutreachJob(
        status="pending",
        job_type="analyze_and_draft" if generate_drafts else "analyze",
        contact_ids=unique_ids,
        total=len(unique_ids),
        completed=0,
        failed=0,
        generate_drafts=generate_drafts,
        custom_instructions=custom_instructions,
        target_use_case=target_use_case,
        force=force,
        results=[],
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


async def run_batch_job(job_id: str) -> None:
    """Process a batch outreach intelligence job in its own DB session."""
    db = SessionLocal()
    try:
        job = db.query(OutreachJob).filter(OutreachJob.id == job_id).one_or_none()
        if not job:
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        db.commit()

        results: list[dict] = list(job.results or [])
        for contact_id in job.contact_ids or []:
            item: dict = {"contact_id": contact_id, "status": "ok"}
            try:
                if job.generate_drafts:
                    draft, intel = await generate_personalized_draft(
                        db,
                        contact_id,
                        custom_instructions=job.custom_instructions,
                        target_use_case=job.target_use_case,
                        force_analyze=job.force,
                    )
                    item["draft_id"] = draft.id
                    item["intelligence"] = intel
                else:
                    intel = await analyze_contact_intelligence(
                        db,
                        contact_id,
                        force=job.force,
                        target_use_case=job.target_use_case,
                    )
                    item["intelligence"] = intel
                job.completed += 1
            except Exception as exc:
                job.failed += 1
                job.completed += 1
                item["status"] = "error"
                item["error"] = str(exc)
            results.append(item)
            job.results = results
            job.updated_at = datetime.utcnow()
            db.commit()
            # Yield so contacts/sync endpoints stay responsive during long batches
            await asyncio.sleep(0)

        job.status = "completed"
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        job = db.query(OutreachJob).filter(OutreachJob.id == job_id).one_or_none()
        if job:
            job.status = "failed"
            job.error_message = str(exc)
            job.completed_at = datetime.utcnow()
            job.updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def get_job(db: Session, job_id: str) -> OutreachJob | None:
    return db.query(OutreachJob).filter(OutreachJob.id == job_id).one_or_none()
