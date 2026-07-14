from __future__ import annotations

import urllib.parse

import httpx
from sqlalchemy.orm import Session

from app.models.message import EmailMessage
from app.services.graph_client import GraphAuthError, GraphClient

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def encode_graph_id(graph_message_id: str) -> str:
    return urllib.parse.quote(graph_message_id, safe="")


def build_owa_weblink(graph_message_id: str) -> str:
    """
    Official OWA format returned by Microsoft Graph.
    Do NOT embed the mailbox email in the path — that breaks multi-account browsers.
    """
    encoded = encode_graph_id(graph_message_id)
    return (
        f"https://outlook.office365.com/owa/?ItemID={encoded}"
        f"&exvsurl=1&viewmodel=ReadMessageItem"
    )


def build_modern_outlook_url(graph_message_id: str) -> str:
    """New Outlook web format without mailbox slug (avoids wrong-account routing)."""
    encoded = encode_graph_id(graph_message_id)
    return f"https://outlook.office.com/mail/sentitems/id/{encoded}"


async def fetch_fresh_weblink(db: Session, graph_message_id: str) -> str | None:
    client = GraphClient(db)
    try:
        token = client.ensure_access_token()
    except GraphAuthError:
        return None
    url = f"{GRAPH_BASE}/me/messages/{graph_message_id}?$select=webLink"
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as http:
        response = await http.get(url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            return None
        return response.json().get("webLink")


async def resolve_outlook_url(db: Session, message: EmailMessage, mailbox: str | None = None) -> str:
    """
    Resolve the best URL to open a sent message in Outlook web for dbains@edgeinvesting.ca.

    Priority:
    1. Stored Graph webLink (tenant-correct OWA link)
    2. Fresh webLink from Graph API
    3. Reconstructed OWA link from graph message id
    4. Modern outlook.office.com link (fallback)
    """
    _ = mailbox  # never embed mailbox in URL — causes wrong account in multi-login browsers

    graph_id = message.graph_message_id
    if graph_id.startswith("test-"):
        return build_owa_weblink(graph_id)

    if message.outlook_weblink and "outlook.office" in message.outlook_weblink:
        return message.outlook_weblink

    fresh = await fetch_fresh_weblink(db, graph_id)
    if fresh:
        return fresh

    return build_owa_weblink(graph_id)
