"""P2 mailbox isolation / real OAuth status tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db_session(monkeypatch):
    monkeypatch.setenv("FEATURE_ACCOUNTS_UI", "true")
    monkeypatch.setenv("MAILBOX_STUB_MODE", "false")
    monkeypatch.setenv("FEATURE_ACCOUNT_EDGE", "true")
    monkeypatch.setenv("FEATURE_ACCOUNT_GALAXY", "true")
    monkeypatch.setenv("FEATURE_ACCOUNT_CAREERS", "true")
    monkeypatch.setenv("FEATURE_ACCOUNT_NORTHWYN", "true")
    monkeypatch.setenv("AZURE_CLIENT_ID", "edge-client")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "edge-secret")
    monkeypatch.setenv("AZURE_TENANT_ID", "edge-tenant")
    monkeypatch.setenv("GALAXY_AZURE_CLIENT_ID", "galaxy-client")
    monkeypatch.setenv("GALAXY_AZURE_CLIENT_SECRET", "galaxy-secret")
    monkeypatch.setenv("GALAXY_AZURE_TENANT_ID", "common")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.database import Base
    from app.models.account import ACCOUNT_SEEDS, MailboxAccount
    from app.models.message import EmailMessage  # noqa: F401
    from app.models.sync import AuthToken  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    for seed in ACCOUNT_SEEDS:
        db.add(
            MailboxAccount(
                id=seed["id"],
                display_name=seed["display_name"],
                email=seed["email"],
                provider=seed["provider"],
                blurb=seed["blurb"],
                is_functional=seed["is_functional"],
                default_included=seed["default_included"],
                enabled=True,
                is_stub=False,
                status="not_connected",
                permissions_json={},
            )
        )
    db.commit()
    yield db
    db.close()
    get_settings.cache_clear()


def test_careers_flagged_functional_and_off_by_default(db_session):
    from app.models.account import MailboxAccount

    careers = db_session.get(MailboxAccount, "careers")
    assert careers is not None
    assert careers.is_functional is True
    assert careers.default_included is False


def test_stub_connect_endpoint_gone():
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    client = TestClient(app)
    res = client.post("/api/v1/accounts/northwyn/stub-connect")
    assert res.status_code == 410


def test_gmail_login_url_requires_real_google_oauth(db_session):
    from app.services.connectors.gmail_connector import GmailMailboxConnector

    connector = GmailMailboxConnector(db_session, "northwyn")
    info = connector.get_login_url()
    assert info.get("stub") is False
    assert "login_url" in info
    assert "accounts.google.com" in info["login_url"]
    assert "stub" not in info.get("login_url", "")


def test_galaxy_login_url_uses_microsoft(db_session):
    from app.services.connectors.graph_connector import GraphMailboxConnector

    connector = GraphMailboxConnector(db_session, "galaxy")
    info = connector.get_login_url()
    assert info.get("stub") is False
    assert "login_url" in info
    assert "login.microsoftonline.com" in info["login_url"]


def test_legacy_stub_token_treated_as_disconnected(db_session):
    from app.models.sync import AuthToken
    from app.services.connectors.gmail_connector import GmailMailboxConnector

    db_session.add(
        AuthToken(
            account_id="northwyn",
            access_token="stub-northwyn",
            user_email="dbains@northwyn.com",
        )
    )
    db_session.commit()

    status = GmailMailboxConnector(db_session, "northwyn").status()
    assert status.connected is False
    assert status.status == "not_connected"


def test_edge_token_expiry_marks_reconnect(db_session):
    from app.models.account import MailboxAccount
    from app.models.sync import AuthToken
    from app.services.connectors.graph_connector import GraphMailboxConnector

    db_session.add(
        AuthToken(
            account_id="edge",
            access_token="expired-token",
            refresh_token=None,
            expires_at=datetime.utcnow() - timedelta(hours=1),
            user_email="dbains@edgeinvesting.ca",
        )
    )
    db_session.get(MailboxAccount, "edge").status = "connected"
    db_session.commit()

    status = GraphMailboxConnector(db_session, "edge").status()
    assert status.status == "reconnect_needed"
    assert status.connected is False


def test_disconnect_one_account_leaves_other(db_session):
    from app.models.sync import AuthToken
    from app.services.connectors.graph_connector import GraphMailboxConnector

    db_session.add(
        AuthToken(account_id="galaxy", access_token="tok-g", user_email="a@galaxypharma.net")
    )
    db_session.add(
        AuthToken(account_id="careers", access_token="tok-c", user_email="careers@galaxypharma.net")
    )
    db_session.commit()

    GraphMailboxConnector(db_session, "galaxy").disconnect()

    assert db_session.query(AuthToken).filter(AuthToken.account_id == "galaxy").count() == 0
    assert db_session.query(AuthToken).filter(AuthToken.account_id == "careers").count() == 1


def test_partial_permissions_when_mail_send_missing(db_session, monkeypatch):
    import base64
    import json

    from app.models.account import MailboxAccount
    from app.models.sync import AuthToken
    from app.services.connectors.graph_connector import GraphMailboxConnector

    db_session.add(
        AuthToken(
            account_id="edge",
            access_token="valid-token",
            refresh_token="r",
            expires_at=datetime.utcnow() + timedelta(hours=1),
            user_email="dbains@edgeinvesting.ca",
        )
    )
    db_session.get(MailboxAccount, "edge").status = "connected"
    db_session.commit()

    connector = GraphMailboxConnector(db_session, "edge")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"scp": "Mail.Read User.Read"}).encode())
        .decode()
        .rstrip("=")
    )
    fake_token = f"aa.{payload}.bb"
    monkeypatch.setattr(connector.graph, "ensure_access_token", lambda: fake_token)

    status = connector.status()
    assert status.connected is True
    assert status.partial_permissions is True
    assert status.can_send is False
