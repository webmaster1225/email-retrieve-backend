from __future__ import annotations

from app.config import get_settings
from app.services.text_utils import extract_domain, format_name_from_email

PERSONAL_LABEL = "Personal email / Unknown company"


def domain_to_company(domain: str) -> str:
    if not domain:
        return PERSONAL_LABEL
    base = domain.split(".")[0]
    base = base.replace("-", " ").replace("_", " ")
    return base.title()


def resolve_company(email: str, full_name: str | None = None) -> tuple[str, str | None, bool, bool]:
    """Return company_name, company_domain, is_internal, is_personal_email."""
    settings = get_settings()
    domain = extract_domain(email)
    if not domain:
        return PERSONAL_LABEL, None, False, False

    is_internal = domain in settings.internal_domain_set or any(
        domain.endswith(f".{internal}") for internal in settings.internal_domain_set
    )
    is_personal = domain in settings.personal_domain_set

    if is_internal:
        return "Edge / Galaxy (Internal)", domain, True, False
    if is_personal:
        return PERSONAL_LABEL, domain, False, True

    company_name = domain_to_company(domain)
    return company_name, domain, False, False


def best_display_name(existing: str | None, candidate: str | None, email: str) -> str:
    if candidate and candidate.lower() != email.lower() and "@" not in candidate:
        if existing and len(existing) >= len(candidate):
            return existing
        return candidate
    if existing:
        return existing
    return format_name_from_email(email)
