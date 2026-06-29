"""Lark IM API client (Lambda side): send / reply / list chat history + CardKit.

``list_chat_messages`` calls the same GET
/im/v1/messages the lark-cli MCP wraps underneath, here with the bot's
tenant token. CardKit (id_convert / update_card / send|reply_card_message)
powers the streaming "正在思考…/🔧 tool" placeholder that updates live, so the
user sees an instant ack instead of waiting for the full reply.
"""

import json
import logging
import re

import requests
from requests import HTTPError

from lark_auth import LARK_OPEN_BASE, get_tenant_access_token, invalidate_token_cache

logger = logging.getLogger(__name__)

LARK_API_BASE = f"{LARK_OPEN_BASE}/open-apis"

_LARK_ID_RE = re.compile(r"^[a-zA-Z0-9_]+$")


def _validate_id(value: str, name: str) -> None:
    if not value or not _LARK_ID_RE.match(value):
        raise ValueError(f"Invalid {name}: must be alphanumeric")


def _authed_request(method: str, url: str, **kwargs) -> requests.Response:
    """Send a Lark API request with automatic token refresh on 401."""
    kwargs.setdefault("timeout", 10)
    kwargs.setdefault("verify", True)
    kwargs.setdefault("headers", {})
    kwargs["headers"]["Authorization"] = f"Bearer {get_tenant_access_token()}"

    resp = requests.request(method, url, **kwargs)
    if resp.status_code == 401:
        logger.warning("Lark API 401, refreshing token and retrying")
        invalidate_token_cache()
        kwargs["headers"]["Authorization"] = f"Bearer {get_tenant_access_token()}"
        resp = requests.request(method, url, **kwargs)
    return resp


def send_text_message(chat_id: str, text: str) -> dict:
    """Send a text message to a Lark chat."""
    _validate_id(chat_id, "chat_id")
    resp = _authed_request(
        "POST",
        f"{LARK_API_BASE}/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text})},
    )
    try:
        resp.raise_for_status()
    except HTTPError:
        raise RuntimeError("Failed to send message: HTTP %d" % resp.status_code) from None
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError("Failed to send message: code=%s" % data.get("code"))
    return data


def reply_text_message(message_id: str, text: str) -> dict:
    """Reply to a specific message, creating or continuing a thread."""
    _validate_id(message_id, "message_id")
    resp = _authed_request(
        "POST",
        f"{LARK_API_BASE}/im/v1/messages/{message_id}/reply",
        json={"msg_type": "text", "content": json.dumps({"text": text})},
    )
    try:
        resp.raise_for_status()
    except HTTPError:
        raise RuntimeError("Failed to reply message: HTTP %d" % resp.status_code) from None
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError("Failed to reply message: code=%s" % data.get("code"))
    return data


def list_chat_messages(chat_id: str, page_size: int = 20, sort: str = "ByCreateTimeDesc") -> list[dict]:
    """List recent messages in a chat (GET /im/v1/messages, container_id_type=chat).

    Returns a list of message dicts (newest first by default). Best-effort —
    returns [] on failure (logged, never raises). The bot must be a member of
    the chat for the tenant token to have access.
    """
    _validate_id(chat_id, "chat_id")
    page_size = max(1, min(page_size, 50))
    try:
        resp = _authed_request(
            "GET",
            f"{LARK_API_BASE}/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": page_size,
                "sort_type": sort,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("list_chat_messages failed: code=%s msg=%s", data.get("code"), data.get("msg"))
            return []
        return data.get("data", {}).get("items", []) or []
    except Exception:
        logger.exception("Exception in list_chat_messages(%s)", chat_id)
        return []


def iter_chat_messages_since(chat_id: str, start_time_ms: int, max_msgs: int = 200, page_size: int = 50) -> list[dict]:
    """All messages created strictly after ``start_time_ms`` (ascending), paginated.

    Used by the hourly ambient-memory consolidator to pull only the chatter since
    its per-chat cursor. Lark's ``start_time`` filter is second-granular and
    inclusive, so the boundary message can re-appear — we keep the cursor in ms
    and re-filter ``create_time > start_time_ms`` to dedupe it. Best-effort:
    returns [] on failure (logged, never raises).
    """
    _validate_id(chat_id, "chat_id")
    start_ms = max(0, int(start_time_ms))
    start_s = start_ms // 1000
    out: list[dict] = []
    page_token = None
    pages = max(1, (max_msgs // max(1, page_size)) + 1)
    try:
        for _ in range(pages):
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": min(page_size, 50),
                "sort_type": "ByCreateTimeAsc",
            }
            if start_s:
                params["start_time"] = str(start_s)
            if page_token:
                params["page_token"] = page_token
            resp = _authed_request("GET", f"{LARK_API_BASE}/im/v1/messages", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("iter_chat_messages_since failed: code=%s msg=%s", data.get("code"), data.get("msg"))
                break
            d = data.get("data", {})
            for it in d.get("items", []) or []:
                if int(it.get("create_time", "0") or 0) > start_ms:
                    out.append(it)
                    if len(out) >= max_msgs:
                        return out
            page_token = d.get("page_token") if d.get("has_more") else None
            if not page_token:
                break
    except Exception:
        logger.exception("Exception in iter_chat_messages_since(%s)", chat_id)
    return out


_RESOURCE_KEY_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")  # image_key/file_key allow hyphens


def download_message_resource(message_id: str, file_key: str, res_type: str = "image") -> tuple[bytes, str]:
    """Download an image/file resource attached to a message.

    Returns (bytes, media_type). Needs scope im:resource. ``res_type`` is "image"
    or "file" per the Lark message-resource API.
    """
    _validate_id(message_id, "message_id")
    if not file_key or not _RESOURCE_KEY_RE.match(file_key):
        raise ValueError("Invalid file_key")
    resp = _authed_request(
        "GET",
        f"{LARK_API_BASE}/im/v1/messages/{message_id}/resources/{file_key}",
        params={"type": res_type},
    )
    resp.raise_for_status()
    media_type = (resp.headers.get("Content-Type") or "image/png").split(";")[0].strip()
    return resp.content, media_type


# ---------------------------------------------------------------------------
# CardKit — inline card send / id_convert / live update (streaming placeholder)
# ---------------------------------------------------------------------------

def _build_card_json(content: str, title: str | None = None) -> str:
    """Build a card JSON 2.0 string with a markdown body. update_multi enables
    in-place updates via the CardKit update endpoint."""
    card = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }
    if title:
        card["header"] = {"title": {"tag": "plain_text", "content": title}}
    return json.dumps(card)


def mention_markdown(open_id: str) -> str:
    """Card-markdown @-mention prefix for a user.

    Verified Feishu/Lark card-JSON 2.0 syntax: a `markdown` element @-mentions a
    user with `<at id=OPEN_ID></at>` (bots support open_id / user_id; `<at id=all></at>`
    is @everyone). Returns '' when open_id is empty so callers can prepend it
    unconditionally.
    """
    return f"<at id={open_id}></at> " if open_id else ""


def id_convert_card(message_id: str) -> str | None:
    """Convert a sent card's message_id to a CardKit card_id (needed to update it)."""
    _validate_id(message_id, "message_id")
    try:
        resp = _authed_request(
            "POST",
            f"{LARK_API_BASE}/cardkit/v1/cards/id_convert",
            json={"message_id": message_id},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("code") != 0:
            logger.error("id_convert_card failed: %s", data if isinstance(data, dict) else type(data).__name__)
            return None
        inner = data.get("data")
        return inner.get("card_id") if isinstance(inner, dict) else None
    except Exception:
        logger.exception("Exception in id_convert_card")
        return None


def update_card(card_id: str, content: str, sequence: int) -> bool:
    """Update a CardKit card entity in place. ``sequence`` must strictly increase."""
    _validate_id(card_id, "card_id")
    try:
        resp = _authed_request(
            "PUT",
            f"{LARK_API_BASE}/cardkit/v1/cards/{card_id}",
            json={
                "card": {"type": "card_json", "data": _build_card_json(content)},
                "sequence": sequence,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.warning("update_card %s failed: code=%s msg=%s", card_id, data.get("code"), data.get("msg"))
            return False
        return True
    except Exception:
        logger.warning("Exception updating card %s", card_id, exc_info=True)
        return False


def send_card_message(chat_id: str, content: str, title: str = "Claude") -> str | None:
    """Send an inline interactive card. Returns message_id or None on failure."""
    _validate_id(chat_id, "chat_id")
    try:
        resp = _authed_request(
            "POST",
            f"{LARK_API_BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive", "content": _build_card_json(content, title)},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.error("send_card_message failed: code=%s msg=%s", data.get("code"), data.get("msg"))
            return None
        return data.get("data", {}).get("message_id")
    except Exception:
        logger.exception("Exception sending card message")
        return None


def reply_card_message(message_id: str, content: str, title: str = "Claude") -> str | None:
    """Reply with an inline interactive card (creates/continues a thread).
    Returns the new card message_id or None on failure."""
    _validate_id(message_id, "message_id")
    try:
        resp = _authed_request(
            "POST",
            f"{LARK_API_BASE}/im/v1/messages/{message_id}/reply",
            json={"msg_type": "interactive", "content": _build_card_json(content, title)},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.error("reply_card_message failed: code=%s msg=%s", data.get("code"), data.get("msg"))
            return None
        return data.get("data", {}).get("message_id")
    except Exception:
        logger.exception("Exception replying card message")
        return None
