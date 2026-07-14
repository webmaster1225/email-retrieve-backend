from __future__ import annotations

import re

RE_PREFIX = re.compile(r"^(re|fw|fwd|aw|sv):\s*", re.IGNORECASE)

NOISE_EMAIL_PATTERNS = (
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "mailer-daemon@",
    "postmaster@",
    "notifications@",
    "newsletter@",
    "bounce@",
    "automated@",
)

TRIVIAL_PREVIEW_PATTERNS = (
    r"^thanks[\s!.&,-]*$",
    r"^thank you[\s!.&,-]*$",
    r"^sounds good[\s!.&,-]*$",
    r"^yes[\s!.&,-]*$",
    r"^ok[\s!.&,-]*$",
    r"^okay[\s!.&,-]*$",
    r"^got it[\s!.&,-]*$",
    r"^perfect[\s!.&,-]*$",
    r"^will do[\s!.&,-]*$",
    r"^thanks\s*&\s*regards[\s!.&,-]*$",
)


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    return email.strip().lower()


def extract_domain(email: str) -> str | None:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        return None
    return normalized.split("@", 1)[1]


def normalize_subject(subject: str | None) -> str:
    if not subject:
        return ""
    value = subject.strip()
    while True:
        updated = RE_PREFIX.sub("", value).strip()
        if updated == value:
            break
        value = updated
    return value.lower()


def parse_display_name(recipient: dict) -> tuple[str | None, str | None]:
    # Graph API format: {"emailAddress": {"name": "...", "address": "..."}}
    email_address = recipient.get("emailAddress") or {}
    email = normalize_email(email_address.get("address"))
    name = (email_address.get("name") or "").strip() or None
    if email:
        return name, email
    # Serialized DB format: {"name": "...", "address": "..."}
    email = normalize_email(recipient.get("address"))
    name = (recipient.get("name") or "").strip() or None
    return name, email


def is_noise_email(email: str) -> bool:
    lower = email.lower()
    return any(pattern in lower for pattern in NOISE_EMAIL_PATTERNS)


def is_trivial_preview(preview: str | None) -> bool:
    if not preview:
        return True
    text = preview.strip()
    if len(text) < 40:
        for pattern in TRIVIAL_PREVIEW_PATTERNS:
            if re.match(pattern, text, re.IGNORECASE):
                return True
    return False


def format_name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    local = re.sub(r"[._-]+", " ", local)
    return local.title()
