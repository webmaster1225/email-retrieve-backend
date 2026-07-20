"""External research providers — swappable, Relationship-Only safe default."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class IdentitySignature:
    name: str | None
    org: str | None
    email: str | None
    domain: str | None = None
    title: str | None = None

    def query_parts(self) -> list[str]:
        parts = [p for p in (self.name, self.org, self.title) if p]
        return parts


@dataclass
class RawHit:
    title: str
    url: str
    snippet: str
    publisher: str | None = None
    publication_date: datetime | None = None
    match_signals: list[str] = field(default_factory=list)


class ResearchProvider(Protocol):
    name: str

    def search_person(self, identity: IdentitySignature) -> list[RawHit]:
        ...


class RelationshipOnlyProvider:
    """Never calls the web — permanent safe fallback."""

    name = "relationship_only"

    def search_person(self, identity: IdentitySignature) -> list[RawHit]:
        return []


class StubResearchProvider:
    """Deterministic hits for tests."""

    name = "stub"

    def __init__(self, hits_by_name: dict[str, list[RawHit]] | None = None):
        self.hits_by_name = hits_by_name or {}
        self.call_count = 0

    def search_person(self, identity: IdentitySignature) -> list[RawHit]:
        self.call_count += 1
        key = (identity.name or "").lower()
        if key in self.hits_by_name:
            return list(self.hits_by_name[key])
        if not identity.name:
            return []
        # Default: one hit matching name+org for identity confirmation tests
        org = identity.org or "Unknown"
        return [
            RawHit(
                title=f"{identity.name} joins {org}",
                url=f"https://example.com/press/{identity.name.replace(' ', '-').lower()}",
                snippet=f"{identity.name} of {org} announced a fund close in May 2025.",
                publisher="Example Press",
                publication_date=datetime(2025, 6, 1),
                match_signals=["name", "org"],
            )
        ]


class HttpAnthropicResearchProvider:
    """Optional light web path — fetches nothing aggressive; returns empty without network keys.

    Real scraping can be layered later; for now this provider documents the seam and
    returns [] so Light/Standard still completes with honest 'nothing found' unless stubbed.
    """

    name = "http_anthropic"

    def search_person(self, identity: IdentitySignature) -> list[RawHit]:
        # Intentionally conservative: no unauthenticated scraping of arbitrary sites.
        # Wire Oxylabs/Apify here later. Identity signature only would be sent.
        _ = identity.query_parts()
        return []


def get_research_provider(provider_name: str | None = None) -> ResearchProvider:
    from app.config import get_settings

    name = (provider_name or get_settings().research_provider or "relationship_only").lower()
    if name in ("relationship_only", "none", "off"):
        return RelationshipOnlyProvider()
    if name == "stub":
        return StubResearchProvider()
    if name in ("http_anthropic", "http", "anthropic"):
        return HttpAnthropicResearchProvider()
    return RelationshipOnlyProvider()
