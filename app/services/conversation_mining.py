"""Conversation mining — strip scaffolding and extract salient human signal.

Uses body_preview (stored snippet) rather than full MIME. De-quotes reply
history, drops signatures/disclaimers, and picks one salient sentence for hooks.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

QUOTE_LINE_RE = re.compile(r"^>+\s?")
FROM_BLOCK_RE = re.compile(
    r"(?:^|\n)[-_]{2,}\s*\n.*|"
    r"(?:^|\n)On .+ wrote:\s*\n.*|"
    r"(?:^|\n)From:\s.+\n.*|"
    r"(?:^|\n)Sent:\s.+\n.*|"
    r"(?:^|\n)-----Original Message-----.*",
    re.I | re.S,
)
SIGNATURE_RE = re.compile(
    r"(?:^|\n)--\s*\n.*|"
    r"(?:^|\n)Best regards?,?\s*\n.*|"
    r"(?:^|\n)Kind regards?,?\s*\n.*|"
    r"(?:^|\n)Thanks?,?\s*\n[A-Z][a-z]+.*|"
    r"(?:^|\n)Sent from my (?:iPhone|iPad|Android).*|"
    r"(?:^|\n)Get Outlook for .*",
    re.I | re.S,
)
DISCLAIMER_RE = re.compile(
    r"(?:confidentiality|this (?:email|message) (?:and any|is confidential)|"
    r"intended solely for|please consider the environment|"
    r"virus[- ]free|disclaimer).*$",
    re.I | re.S,
)
MEETING_BOILERPLATE_RE = re.compile(
    r"(?:microsoft teams|zoom meeting|when:\s|where:\s|join (?:the )?meeting|"
    r"meeting id:|passcode:|dial[- ]in)",
    re.I,
)
COMMITMENT_RE = re.compile(
    r"\b(i(?:'|’)?ll\s+send|let(?:'|’)?s\s+reconnect|looking\s+forward|"
    r"happy\s+to\s+(?:intro|connect|chat)|circle\s+back|follow\s+up|"
    r"next\s+steps?|co-?invest|fund\s+(?:iv|v|close)|congrats)\b",
    re.I,
)


def strip_quoted_history(text: str | None) -> str:
    if not text:
        return ""
    cleaned = FROM_BLOCK_RE.split(text, maxsplit=1)[0]
    lines = []
    for line in cleaned.splitlines():
        if QUOTE_LINE_RE.match(line.strip()):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def strip_signature_and_disclaimer(text: str | None) -> str:
    if not text:
        return ""
    cleaned = SIGNATURE_RE.split(text, maxsplit=1)[0]
    cleaned = DISCLAIMER_RE.sub("", cleaned)
    return cleaned.strip()


def clean_message_body(preview: str | None) -> str:
    """Original human text only — no quotes, signatures, or disclaimers."""
    text = strip_quoted_history(preview)
    text = strip_signature_and_disclaimer(text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_salient_sentence(text: str | None, *, max_len: int = 180) -> str | None:
    """Pick the most relationship-useful sentence from cleaned body text."""
    cleaned = clean_message_body(text)
    if not cleaned:
        return None
    if MEETING_BOILERPLATE_RE.search(cleaned) and len(cleaned) < 80:
        return None

    # Split into sentences (keep short clauses)
    parts = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    candidates = [p.strip(" \t-–—") for p in parts if p and len(p.strip()) >= 20]
    if not candidates:
        snippet = cleaned[:max_len].strip()
        return snippet or None

    def score(s: str) -> float:
        sc = min(len(s), 160) / 40.0
        if COMMITMENT_RE.search(s):
            sc += 3.0
        if MEETING_BOILERPLATE_RE.search(s):
            sc -= 2.0
        if s.lower().startswith(("hi ", "hello ", "dear ", "thanks", "thank you")):
            sc -= 0.5
        return sc

    best = max(candidates, key=score)
    if len(best) > max_len:
        best = best[: max_len - 1].rstrip() + "…"
    return best


def original_text_volume(preview: str | None) -> int:
    return len(clean_message_body(preview) or "")


def substance_score(
    *,
    subject: str | None,
    preview: str | None,
    direction: str | None,
    has_inbound_peer: bool = False,
) -> float:
    """Rank messages by two-way signal, original text volume, then leave room for recency."""
    vol = original_text_volume(preview)
    score = min(vol, 400) / 40.0  # up to ~10
    salient = extract_salient_sentence(preview)
    if salient:
        score += 2.0
        if COMMITMENT_RE.search(salient):
            score += 2.0
    if has_inbound_peer:
        score += 3.0
    if (direction or "").lower() == "inbound":
        score += 1.5
    # Prefer non-FW subjects when body is thin
    subj = (subject or "").lower()
    if subj.startswith(("fw:", "fwd:")) and vol < 40:
        score -= 2.0
    return score


def rank_substantive_messages(
    messages: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Sort surviving messages: substance first, then recency as tie-breaker."""
    now = now or datetime.utcnow()
    directions = {(m.get("direction") or "").lower() for m in messages}
    two_way = "inbound" in directions and "outbound" in directions

    scored: list[tuple[float, dict[str, Any]]] = []
    for m in messages:
        when = m.get("occurred_at") or m.get("sent_datetime")
        age_days = 365.0
        if isinstance(when, datetime):
            age_days = max(0.0, (now - when).total_seconds() / 86400.0)
        # Recency component: newer is better, but cannot alone beat substance
        recency = max(0.0, 5.0 - age_days / 180.0)
        base = substance_score(
            subject=m.get("subject"),
            preview=m.get("body_preview") or m.get("preview") or m.get("summary"),
            direction=m.get("direction"),
            has_inbound_peer=two_way,
        )
        scored.append((base + recency, m))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [m for _, m in scored]


def mine_hook_line(item: dict[str, Any]) -> str:
    """Human-facing hook fragment from a mined evidence item (not raw subject)."""
    salient = item.get("salient") or extract_salient_sentence(
        item.get("summary") or item.get("body_preview")
    )
    when = item.get("occurred_at")
    when_s = when.strftime("%b %Y") if isinstance(when, datetime) else None
    if salient:
        line = salient.rstrip(".")
        if when_s:
            return f"{line} ({when_s})"
        return line
    subj = item.get("subject") or "our last exchange"
    if when_s:
        return f"{subj} ({when_s})"
    return str(subj)
