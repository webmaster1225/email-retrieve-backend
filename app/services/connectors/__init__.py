from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.account import ACCOUNT_CAREERS, ACCOUNT_EDGE, ACCOUNT_GALAXY, ACCOUNT_NORTHWYN
from app.services.connectors.gmail_connector import GmailMailboxConnector
from app.services.connectors.graph_connector import GraphMailboxConnector


def get_connector(db: Session, account_id: str):
    settings = get_settings()
    if not settings.account_feature_enabled(account_id):
        raise KeyError(f"Account feature disabled: {account_id}")

    if account_id == ACCOUNT_NORTHWYN:
        return GmailMailboxConnector(db, account_id=ACCOUNT_NORTHWYN)

    if account_id in (ACCOUNT_EDGE, ACCOUNT_GALAXY, ACCOUNT_CAREERS):
        return GraphMailboxConnector(db, account_id=account_id)

    raise KeyError(f"Unknown account: {account_id}")


def list_enabled_account_ids() -> list[str]:
    settings = get_settings()
    ids = [ACCOUNT_EDGE, ACCOUNT_NORTHWYN, ACCOUNT_GALAXY, ACCOUNT_CAREERS]
    return [i for i in ids if settings.account_feature_enabled(i)]
