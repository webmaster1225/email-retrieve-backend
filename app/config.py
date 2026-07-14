from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


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
        env_file=str(BASE_DIR / ".env"),
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
    database_url: str = Field(default_factory=_default_database_url)

    # App
    frontend_url: str = "http://localhost:3000"
    cors_origins: str = ""
    secret_key: str = "change-me-in-production"

    # Anthropic (MVP 4 — on-demand AI)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # Domain config (comma-separated)
    internal_domains: str = "edgeinvesting.ca,galaxypharma.com,galaxypharma.ca,galaxypharma.net"
    personal_domains: str = (
        "gmail.com,outlook.com,hotmail.com,yahoo.com,icloud.com,live.com,"
        "me.com,protonmail.com,proton.me,aol.com"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
