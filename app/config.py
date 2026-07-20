from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = BACKEND_DIR.parent


def _default_database_url() -> str:
    # Azure App Service persists /home — use it for SQLite in production
    if os.getenv("WEBSITE_SITE_NAME"):
        data_dir = Path("/home/data")
        data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{data_dir / 'crm.db'}"
    data_dir = BASE_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'crm.db'}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Prefer backend/.env (later file wins); root .env still works as fallback
        env_file=(str(BASE_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Microsoft Graph / Azure AD
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = "common"
    azure_redirect_uri: str = "http://localhost:8000/api/v1/auth/callback"
    graph_scopes: str = "Mail.Read,Mail.Send,User.Read"

    # Database (override in Azure: sqlite:////home/data/crm.db)
    # WARNING: SQLite + multiple App Service instances = split-brain / lost writes.
    # Keep API scale-out at 1 instance until migrated to Postgres.
    database_url: str = Field(default_factory=_default_database_url)

    # App
    frontend_url: str = "http://localhost:3000"
    cors_origins: str = ""
    secret_key: str = "change-me-in-production"

    # Treat a sync as hung if still "running" longer than this (hours).
    # Startup cleanup handles restarts; this covers wedged in-process jobs.
    sync_stale_hours: float = 6.0

    # Anthropic (MVP 4 — on-demand AI)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # Domain config (comma-separated)
    internal_domains: str = "edgeinvesting.ca,galaxypharma.com,galaxypharma.ca,galaxypharma.net"
    personal_domains: str = (
        "gmail.com,outlook.com,hotmail.com,yahoo.com,icloud.com,live.com,"
        "me.com,protonmail.com,proton.me,aol.com"
    )

    # P2 — multi-mailbox feature flags
    feature_accounts_ui: bool = True
    feature_account_edge: bool = True
    feature_account_galaxy: bool = True
    feature_account_careers: bool = True
    feature_account_northwyn: bool = True
    # Stub mode removed — always require real OAuth
    mailbox_stub_mode: bool = False

    # P3–P5 Compass live campaigns
    feature_compass_campaigns: bool = False
    # P9 Gate 8 send (save/schedule still work when false)
    feature_compass_send: bool = False
    # P10 campaign reply tracking
    feature_compass_tracking: bool = False
    # P11 follow-up proposals (Gate 9); send still needs feature_compass_send
    feature_compass_followups: bool = False
    followup_no_response_days: int = 7

    # P6–P7 research / drafting
    research_provider: str = "relationship_only"  # relationship_only|stub|http_anthropic
    research_mode_default: str = "relationship_only"
    linkedin_signature_url: str = ""
    # Gate 7 schedule sweep interval (seconds)
    schedule_sweep_seconds: float = 60.0

    # Public API base used to build OAuth redirect URIs
    api_public_base: str = "http://localhost:8000"

    # Google / Gmail (Northwyn)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/accounts/northwyn/oauth/callback"
    google_scopes: str = (
        "openid email profile "
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.send "
        "https://www.googleapis.com/auth/gmail.compose"
    )

    # Galaxy tenant Graph (Galaxy Pharma + Careers)
    galaxy_azure_client_id: str = ""
    galaxy_azure_client_secret: str = ""
    galaxy_azure_tenant_id: str = ""
    galaxy_azure_redirect_uri: str = ""  # optional; defaults per-account callback

    # Optional Edge per-account callback (Settings → /accounts/edge/…)
    edge_account_redirect_uri: str = (
        "http://localhost:8000/api/v1/accounts/edge/oauth/callback"
    )

    @property
    def internal_domain_set(self) -> set[str]:
        return {d.strip().lower() for d in self.internal_domains.split(",") if d.strip()}

    @property
    def personal_domain_set(self) -> set[str]:
        return {d.strip().lower() for d in self.personal_domains.split(",") if d.strip()}

    @property
    def graph_scope_list(self) -> list[str]:
        normalized = self.graph_scopes.replace(",", " ")
        return [scope for part in normalized.split() if (scope := part.strip())]

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip():
            return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        origins = {self.frontend_url.rstrip("/"), "http://localhost:3000", "http://127.0.0.1:3000"}
        return sorted(origins)

    def account_feature_enabled(self, account_id: str) -> bool:
        mapping = {
            "edge": self.feature_account_edge,
            "galaxy": self.feature_account_galaxy,
            "careers": self.feature_account_careers,
            "northwyn": self.feature_account_northwyn,
        }
        return bool(mapping.get(account_id, False))


@lru_cache
def get_settings() -> Settings:
    return Settings()