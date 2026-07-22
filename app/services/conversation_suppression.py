"""Conversation suppression — drop Bid-blast / automated mail from signal selection.

Seeded with Edge Investing Bid Invitation patterns and common automated-mail
signatures. Campaigns may append extra suppressions via plan_json or
campaign.message_strategy["suppressed_subjects"].
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.services.text_utils import normalize_subject

# Accidental Bid Invitation spam blast (audit: 68% of hooks)
BID_SUPPRESSION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"bid\s+invitat",  # Bid Invitation / Bid Invitaton (typos in live data)
        r"collaborat\w*\s+proposal",
        r"edge\s+investing\s*[-—:]?\s*bid",
        r"edge\s+investing\s+invitat",
        r"proposal\s+request.*bid\s*#?\s*rw",
        r"invitation\s+for\s+bid\s*#",
        r"community\s+infrastructure",
        r"bid\s*#\s*rw\d+",
    )
)

# Calendar, NDR, auto-replies, newsletters (audit: 25% of hooks)
AUTOMATED_SUBJECT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.I)
    for p in (
        r"^accepted:",
        r"^canceled:",
        r"^cancelled:",
        r"^declined:",
        r"^tentative:",
        r"^updated\s+invitation:",
        r"^invitation:",
        r"^automatic\s+reply",
        r"^auto[- ]?reply",
        r"^out\s+of\s+office",
        r"^ooo:",
        r"^undeliverable",
        r"^delivery\s+status\s+notification",
        r"^failure\s+notice",
        r"^returned\s+mail",
        r"^mail\s+delivery\s+failed",
        r"^read:\s*",
        r"^delivery\s+receipt",
        r"^newsletter",
        r"^unsubscribe",
    )
)

NO_REPLY_SENDER_RE = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster|"
    r"notifications?|newsletter|bounce|automated|calendar-notification)@",
    re.I,
)


def _normalize_extra(patterns: Iterable[str] | None) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for raw in patterns or []:
        text = (raw or "").strip()
        if not text:
            continue
        try:
            out.append(re.compile(re.escape(text), re.I))
        except re.error:
            continue
    return out


def collect_extra_suppressions(plan: dict[str, Any] | None = None) -> list[str]:
    """User / campaign suppressions from plan or message_strategy."""
    plan = plan or {}
    extras: list[str] = []
    for key in ("suppressed_subjects", "suppress_threads", "ignored_threads"):
        val = plan.get(key)
        if isinstance(val, list):
            extras.extend(str(x) for x in val if x)
        elif isinstance(val, str) and val.strip():
            extras.append(val.strip())
    return extras


def is_suppressed_subject(
    subject: str | None,
    *,
    extra_patterns: Iterable[str] | None = None,
) -> bool:
    """True if subject matches Bid blast, automated mail, or user suppressions."""
    if not subject or not str(subject).strip():
        return False
    raw = str(subject)
    norm = normalize_subject(raw)
    blob = f"{raw} {norm}"
    for pat in BID_SUPPRESSION_PATTERNS:
        if pat.search(blob):
            return True
    for pat in AUTOMATED_SUBJECT_PATTERNS:
        if pat.search(raw) or pat.search(norm):
            return True
    for pat in _normalize_extra(extra_patterns):
        if pat.search(blob):
            return True
    return False


def is_automated_sender(email: str | None) -> bool:
    if not email:
        return False
    return bool(NO_REPLY_SENDER_RE.search(email.strip()))


def is_suppressed_message(
    *,
    subject: str | None,
    sender_email: str | None = None,
    extra_patterns: Iterable[str] | None = None,
) -> bool:
    if is_automated_sender(sender_email):
        return True
    return is_suppressed_subject(subject, extra_patterns=extra_patterns)


def suppression_reason(
    subject: str | None,
    *,
    sender_email: str | None = None,
    extra_patterns: Iterable[str] | None = None,
) -> str | None:
    if is_automated_sender(sender_email):
        return "automated_sender"
    if not subject:
        return None
    raw = str(subject)
    norm = normalize_subject(raw)
    blob = f"{raw} {norm}"
    for pat in BID_SUPPRESSION_PATTERNS:
        if pat.search(blob):
            return "bid_invitation_blast"
    for pat in AUTOMATED_SUBJECT_PATTERNS:
        if pat.search(raw) or pat.search(norm):
            return "automated_mail"
    for pat in _normalize_extra(extra_patterns):
        if pat.search(blob):
            return "user_suppressed"
    return None


def add_suppression_to_plan(plan: dict[str, Any], subject: str) -> dict[str, Any]:
    """Append a subject signature for one-click 'ignore this thread'."""
    out = dict(plan or {})
    sig = normalize_subject(subject) or (subject or "").strip()
    if not sig:
        return out
    existing = list(out.get("suppressed_subjects") or [])
    # Store a short distinctive fragment for matching
    fragment = sig[:120]
    if fragment.lower() not in {e.lower() for e in existing}:
        existing.append(fragment)
    out["suppressed_subjects"] = existing
    return out
