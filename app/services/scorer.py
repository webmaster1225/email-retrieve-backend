from __future__ import annotations

INVESTOR_DOMAIN_KEYWORDS = (
    "capital",
    "ventures",
    "venture",
    "partners",
    "investment",
    "investments",
    "familyoffice",
    "family-office",
    "holdings",
    "equity",
    "fund",
    "advisory",
    "wealth",
    "asset",
    "privateequity",
    "pe.",
)

TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "fundraising": (
        "investor",
        "raise",
        "capital",
        "funding",
        "deck",
        "teaser",
        "round",
        "cim",
        "term sheet",
        "valuation",
        "family office",
    ),
    "pharma": ("pharma", "healthcare", "503b", "galaxy pharma", "formulation", "drug", "clinical"),
    "board": ("board", "director", "governance", "chair"),
    "intro": ("intro", "introduction", "connect you", "meet", "introduce"),
    "meeting": ("call", "meeting", "follow-up", "follow up", "catch up", "schedule"),
}

ENGAGEMENT_SUBJECT_KEYWORDS = ("intro", "call", "meeting", "follow-up", "follow up")


def score_to_tier(score: int) -> str:
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def compute_fundraising_score(
    *,
    company_domain: str | None,
    company_name: str | None,
    subjects: list[str],
    previews: list[str],
    email_count: int,
    last_contacted_at,
    has_attachments: bool,
    is_internal: bool,
    is_personal_email: bool,
    is_excluded: bool,
) -> tuple[int, dict[str, int]]:
    breakdown: dict[str, int] = {}
    score = 0

    if is_excluded:
        breakdown["excluded"] = -20
        return max(score + breakdown["excluded"], 0), breakdown

    domain_blob = f"{company_domain or ''} {company_name or ''}".lower()
    text_blob = " ".join(subjects + previews).lower()

    for keyword in INVESTOR_DOMAIN_KEYWORDS:
        if keyword in domain_blob:
            breakdown["investor_domain"] = 30
            score += 30
            break

    fundraising_hits = sum(1 for kw in TOPIC_KEYWORDS["fundraising"] if kw in text_blob)
    pharma_hits = sum(1 for kw in TOPIC_KEYWORDS["pharma"] if kw in text_blob)
    if fundraising_hits:
        points = min(20, fundraising_hits * 5)
        breakdown["fundraising_keywords"] = points
        score += points
    if pharma_hits:
        points = min(15, pharma_hits * 5)
        breakdown["pharma_keywords"] = points
        score += points

    if email_count >= 2:
        breakdown["multiple_emails"] = 15
        score += 15

    if last_contacted_at:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        last = last_contacted_at.replace(tzinfo=timezone.utc) if last_contacted_at.tzinfo is None else last_contacted_at
        if (now - last).days <= 365:
            breakdown["recent_contact"] = 10
            score += 10

    if has_attachments:
        breakdown["attachments"] = 10
        score += 10

    if any(kw in text_blob for kw in ENGAGEMENT_SUBJECT_KEYWORDS):
        breakdown["engagement_keywords"] = 10
        score += 10

    if is_internal:
        breakdown["internal"] = -30
        score -= 30
    if is_personal_email:
        breakdown["personal_email"] = -10
        score -= 10

    return max(score, 0), breakdown


def detect_topics(subjects: list[str], previews: list[str]) -> list[str]:
    text_blob = " ".join(subjects + previews).lower()
    found: list[str] = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in text_blob for kw in keywords):
            found.append(topic)
    return found


def infer_contact_type(topics: list[str], score: int, domain_blob: str) -> str | None:
    if any(k in domain_blob for k in ("capital", "ventures", "fund", "equity", "familyoffice")):
        return "investor"
    if "fundraising" in topics and score >= 25:
        return "investor"
    if "pharma" in topics:
        return "pharma"
    if "board" in topics:
        return "board"
    if "intro" in topics:
        return "intro"
    if score >= 50:
        return "investor"
    if score >= 25:
        return "business_development"
    return None
