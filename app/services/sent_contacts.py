from __future__ import annotations

from datetime import datetime

from app.services.company_resolver import best_display_name, resolve_company
from app.services.scorer import compute_fundraising_score, detect_topics, score_to_tier
from app.services.text_utils import is_noise_email, normalize_email, parse_display_name


def _matches_search(q: str, full_name: str | None, email: str, company_name: str | None) -> bool:
    needle = q.strip().lower()
    if not needle:
        return True
    haystacks = [email, (full_name or "").lower(), (company_name or "").lower()]
    return any(needle in value for value in haystacks if value)


def extract_contacts_from_messages(
    messages: list[dict],
    *,
    exclude_emails: set[str],
    q: str | None = None,
) -> dict[str, dict]:
    """Build unique external contacts from a page of Graph sent messages."""
    contacts: dict[str, dict] = {}

    for item in messages:
        sent_raw = item.get("sentDateTime")
        if not sent_raw:
            continue
        sent_dt = datetime.fromisoformat(sent_raw.replace("Z", "+00:00"))
        subject = item.get("subject")
        preview = item.get("bodyPreview")
        message_id = item["id"]
        weblink = item.get("webLink")
        conversation_id = item.get("conversationId")
        has_attachments = bool(item.get("hasAttachments"))

        for field in ("toRecipients", "ccRecipients", "bccRecipients"):
            for recipient in item.get(field) or []:
                display_name, email = parse_display_name(recipient)
                email = normalize_email(email)
                if not email or email in exclude_emails or is_noise_email(email):
                    continue

                company_name, company_domain, is_internal, _is_personal = resolve_company(email, display_name)
                if is_internal:
                    continue
                if q and not _matches_search(q, display_name, email, company_name):
                    continue

                company_name, company_domain, is_internal, is_personal = resolve_company(email, display_name)
                if is_internal:
                    continue
                if q and not _matches_search(q, display_name, email, company_name):
                    continue

                existing = contacts.get(email)
                if existing is None:
                    contacts[email] = {
                        "id": email,
                        "full_name": best_display_name(None, display_name, email),
                        "primary_email": email,
                        "company_name": company_name,
                        "company_domain": company_domain,
                        "is_personal_email": is_personal,
                        "last_contacted_at": sent_dt.isoformat(),
                        "last_subject": subject,
                        "last_preview": preview,
                        "latest_message_id": message_id,
                        "latest_outlook_weblink": weblink,
                        "email_count": 1,
                        "thread_count": 1,
                        "has_attachments": has_attachments,
                        "_sent_dt": sent_dt,
                        "_threads": {conversation_id} if conversation_id else set(),
                        "_subjects": [subject or ""],
                        "_previews": [preview or ""],
                    }
                    continue

                existing["email_count"] = existing.get("email_count", 1) + 1
                if conversation_id:
                    existing["_threads"].add(conversation_id)
                    existing["thread_count"] = len(existing["_threads"])
                if has_attachments:
                    existing["has_attachments"] = True
                existing["_subjects"].append(subject or "")
                existing["_previews"].append(preview or "")
                if sent_dt > existing["_sent_dt"]:
                    existing["_sent_dt"] = sent_dt
                    existing["last_contacted_at"] = sent_dt.isoformat()
                    existing["last_subject"] = subject
                    existing["last_preview"] = preview
                    existing["latest_message_id"] = message_id
                    existing["latest_outlook_weblink"] = weblink
                existing["full_name"] = best_display_name(existing["full_name"], display_name, email)

    for contact in contacts.values():
        sent_dt = contact.pop("_sent_dt", None)
        contact.pop("_threads", None)
        subjects = contact.pop("_subjects", [])
        previews = contact.pop("_previews", [])
        # Score from recent samples only — sufficient for list ranking, much faster.
        score, _ = compute_fundraising_score(
            company_domain=contact.get("company_domain"),
            company_name=contact.get("company_name"),
            subjects=subjects[-3:],
            previews=previews[-3:],
            email_count=contact.get("email_count", 1),
            last_contacted_at=sent_dt,
            has_attachments=contact.get("has_attachments", False),
            is_internal=False,
            is_personal_email=contact.get("is_personal_email", False),
            is_excluded=False,
        )
        contact["fundraising_relevance_score"] = score
        contact["fundraising_relevance_tier"] = score_to_tier(score)
        contact["detected_topics"] = detect_topics(subjects[-3:], previews[-3:])
        contact.pop("has_attachments", None)
        contact.pop("is_personal_email", None)
    return contacts
