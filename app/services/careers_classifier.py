"""G-02 — classify Careers mailbox senders before they enter candidate pools."""

from __future__ import annotations

import re
from typing import Literal

CareersClass = Literal[
    "candidate",
    "recruiter",
    "agency",
    "vendor",
    "automated",
    "genuine_relationship",
]

_AGENCY = re.compile(
    r"\b(recruit|staffing|talent|agency|headhunt|search firm|placement)\b",
    re.I,
)
_RECRUITER = re.compile(
    r"\b(recruiter|talent acquisition|hiring manager|sourcer)\b",
    re.I,
)
_CANDIDATE = re.compile(
    r"\b(resume|cv\b|application|applied for|job application|cover letter|interview)\b",
    re.I,
)
_VENDOR = re.compile(
    r"\b(invoice|purchase order|quote|procurement|supplier)\b",
    re.I,
)
_AUTO = re.compile(
    r"\b(noreply|no-reply|donotreply|do-not-reply|automated|notification|"
    r"unsubscribe|mailer-daemon|bounce)\b",
    re.I,
)


def classify_careers_sender(
    *,
    email: str | None,
    name: str | None = None,
    subjects: list[str] | None = None,
    company: str | None = None,
) -> CareersClass:
    """Classify a Careers-mailbox correspondent.

    Volume alone never implies a genuine relationship — recruiting noise is
    filtered before candidate pools (G-02 acceptance).
    """
    blob = " ".join(
        filter(
            None,
            [
                email or "",
                name or "",
                company or "",
                " ".join(subjects or []),
            ],
        )
    )
    local = (email or "").split("@")[0].lower()
    if _AUTO.search(blob) or _AUTO.search(local):
        return "automated"
    if _AGENCY.search(blob):
        return "agency"
    if _RECRUITER.search(blob):
        return "recruiter"
    if _CANDIDATE.search(blob):
        return "candidate"
    if _VENDOR.search(blob):
        return "vendor"
    # Without positive recruiting signals, treat as possible relationship —
    # still low strength unless other mailboxes corroborate.
    return "genuine_relationship"


def is_recruiting_noise(classification: CareersClass) -> bool:
    return classification in ("candidate", "recruiter", "agency", "automated", "vendor")
