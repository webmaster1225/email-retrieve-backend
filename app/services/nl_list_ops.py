"""P5 — natural-language list operations with restate-before-apply."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.campaign import CampaignCandidate


@dataclass
class NlOpPreview:
    instruction: str
    restatement: str
    candidate_ids: list[str] = field(default_factory=list)
    action: str = "pass"  # pass|include|unsure
    matched_count: int = 0


def preview_nl_op(
    db: Session,
    campaign_id: str,
    instruction: str,
) -> NlOpPreview:
    text = (instruction or "").strip()
    lower = text.lower()
    candidates = (
        db.query(CampaignCandidate)
        .filter(CampaignCandidate.campaign_id == campaign_id)
        .order_by(CampaignCandidate.rank.asc())
        .all()
    )

    action = "pass"
    if "include" in lower or "keep only" in lower or "approve" in lower:
        action = "include"
    if "unsure" in lower:
        action = "unsure"

    matched: list[CampaignCandidate] = []
    restatement = ""

    # Stale > N years
    m = re.search(r"(?:stale|haven'?t spoken|older than|more than)\s+(\d+)\s+years?", lower)
    m2 = re.search(r">\s*(\d+)\s+years?", lower)
    years = None
    if m:
        years = int(m.group(1))
    elif m2:
        years = int(m2.group(1))
    elif "three years" in lower or "3 years" in lower:
        years = 3
    elif "stale" in lower:
        years = 3

    if years is not None:
        cutoff = datetime.utcnow() - timedelta(days=365 * years)
        for c in candidates:
            # Use flags or evidence dates via strength label
            if "stale" in (c.flags or []) or c.strength_label == "needs_reconnection":
                matched.append(c)
            elif c.flags and "outside_lookback" in c.flags:
                matched.append(c)
        # Also match by parsing why_text years if needed — keep flag-based
        restatement = (
            f"I'll mark {len(matched)} people you haven't had recent contact with "
            f"(~{years}+ years / flagged stale) as '{action}'."
        )
        action = "pass"
        return NlOpPreview(
            instruction=text,
            restatement=restatement or f"No stale contacts found for {years}+ years.",
            candidate_ids=[c.id for c in matched],
            action=action,
            matched_count=len(matched),
        )

    # Drop bankers
    if "banker" in lower:
        for c in candidates:
            if (c.role_label or "").lower() == "banker" or "bank" in (c.company or "").lower():
                matched.append(c)
        restatement = f"I'll pass on {len(matched)} banker contacts."
        return NlOpPreview(
            instruction=text,
            restatement=restatement,
            candidate_ids=[c.id for c in matched],
            action="pass",
            matched_count=len(matched),
        )

    # Keep only introducers / investors / etc.
    for role in ("introducer", "investor", "advisor", "operator", "customer", "vendor"):
        if f"only {role}" in lower or f"keep {role}" in lower or f"only {role}s" in lower:
            keep = [c for c in candidates if (c.role_label or "").lower() == role]
            drop = [c for c in candidates if c not in keep]
            restatement = (
                f"I'll keep {len(keep)} {role}s and pass on the other {len(drop)}."
            )
            return NlOpPreview(
                instruction=text,
                restatement=restatement,
                candidate_ids=[c.id for c in drop],
                action="pass",
                matched_count=len(drop),
            )

    # High confidence batch include
    if "high confidence" in lower or "strongest" in lower or "batch approve" in lower:
        matched = [
            c
            for c in candidates
            if c.strength_label in ("solid", "strong_relationship")
            and c.relevance_label in ("high_relevance", "medium_relevance")
        ]
        restatement = f"I'll include {len(matched)} high-confidence candidates."
        return NlOpPreview(
            instruction=text,
            restatement=restatement,
            candidate_ids=[c.id for c in matched],
            action="include",
            matched_count=len(matched),
        )

    restatement = (
        "I wasn't sure how to apply that. Try: "
        "'remove anyone stale >3 years', 'drop bankers', or 'keep only introducers'."
    )
    return NlOpPreview(
        instruction=text,
        restatement=restatement,
        candidate_ids=[],
        action="pass",
        matched_count=0,
    )


def apply_nl_op(
    db: Session,
    campaign_id: str,
    preview: NlOpPreview,
) -> dict[str, Any]:
    updated = 0
    for cid in preview.candidate_ids:
        row = db.get(CampaignCandidate, cid)
        if not row or row.campaign_id != campaign_id:
            continue
        row.decision = preview.action
        row.updated_at = datetime.utcnow()
        updated += 1
    db.commit()
    return {
        "updated": updated,
        "action": preview.action,
        "restatement": preview.restatement,
        "candidate_ids": preview.candidate_ids,
    }
