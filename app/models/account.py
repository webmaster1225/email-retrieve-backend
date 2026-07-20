from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# Canonical account ids
ACCOUNT_EDGE = "edge"
ACCOUNT_GALAXY = "galaxy"
ACCOUNT_CAREERS = "careers"
ACCOUNT_NORTHWYN = "northwyn"

ACCOUNT_SEEDS: list[dict] = [
    {
        "id": ACCOUNT_EDGE,
        "display_name": "Edge Investing",
        "email": "dbains@edgeinvesting.ca",
        "provider": "graph",
        "blurb": "Best for investor, capital-markets, and deal relationships",
        "is_functional": False,
        "default_included": True,
    },
    {
        "id": ACCOUNT_GALAXY,
        "display_name": "Galaxy Pharmaceuticals",
        "email": "dalbir.bains@galaxypharma.net",
        "provider": "graph",
        "blurb": "Best for pharma operating partners, vendors, and industry contacts",
        "is_functional": False,
        "default_included": False,
    },
    {
        "id": ACCOUNT_CAREERS,
        "display_name": "Galaxy Careers",
        "email": "careers@galaxypharma.net",
        "provider": "graph",
        "blurb": "Recruiting mailbox - candidates and agencies, rarely personal relationships",
        "is_functional": True,
        "default_included": False,
    },
    {
        "id": ACCOUNT_NORTHWYN,
        "display_name": "Northwyn",
        "email": "dbains@northwyn.com",
        "provider": "gmail",
        "blurb": "Best for Northwyn investors, lenders, acquisition partners, and advisors",
        "is_functional": False,
        "default_included": True,
    },
]


class MailboxAccount(Base):
    __tablename__ = "mailbox_accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(320))
    provider: Mapped[str] = mapped_column(String(16))  # graph | gmail | stub
    blurb: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(32), default="not_connected", index=True
    )  # not_connected | connected | reconnect_needed | syncing
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_plain: Mapped[str | None] = mapped_column(String(128))
    permissions_json: Mapped[dict | None] = mapped_column(JSON, default=dict)
    is_functional: Mapped[bool] = mapped_column(Boolean, default=False)
    default_included: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_stub: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
