"""Minimal Lark API client for the agent container (tenant-token, requests).

Self-contained (the agent container does not share the Lambda's lark-shared
layer). Reads bot credentials from env (injected from Secrets Manager at deploy):
  LARK_APP_ID, LARK_APP_SECRET   (+ optional LARK_OPEN_BASE)

Only the capabilities the agent tools need. Best-effort: callers should expect
exceptions and surface a friendly message.
"""

import json
import os
import time

import requests

LARK_OPEN_BASE = os.environ.get("LARK_OPEN_BASE", "https://open.larksuite.com")
API = f"{LARK_OPEN_BASE}/open-apis"

_token = {"v": None, "exp": 0}


def _tenant_token() -> str:
    now = time.time()
    if _token["v"] and now < _token["exp"]:
        return _token["v"]
    resp = requests.post(
        f"{API}/auth/v3/tenant_access_token/internal",
        json={"app_id": os.environ["LARK_APP_ID"], "app_secret": os.environ["LARK_APP_SECRET"]},
        timeout=5,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tenant token failed: {data.get('code')}")
    _token["v"] = data["tenant_access_token"]
    _token["exp"] = now + data.get("expire", 7200) - 300
    return _token["v"]


def _req(method: str, path: str, **kw) -> dict:
    kw.setdefault("timeout", 15)
    kw.setdefault("headers", {})
    kw["headers"]["Authorization"] = f"Bearer {_tenant_token()}"
    resp = requests.request(method, f"{API}{path}", **kw)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lark API {path} failed: code={data.get('code')} msg={data.get('msg')}")
    return data.get("data", {})


def list_chat_messages(chat_id: str, page_size: int = 20) -> list[dict]:
    """Recent messages in a chat (newest first)."""
    data = _req(
        "GET", "/im/v1/messages",
        params={
            "container_id_type": "chat", "container_id": chat_id,
            "page_size": max(1, min(page_size, 50)), "sort_type": "ByCreateTimeDesc",
        },
    )
    return data.get("items", []) or []


def create_doc(title: str, markdown: str) -> str:
    """Create a Lark Docx document AND fill it with the markdown content.

    Official three-step flow (docx v1):
      1. POST /docx/v1/documents                         -> empty doc, get document_id
      2. POST /docx/v1/documents/blocks/convert          -> markdown -> nested blocks
         (requires scope docx:document.block:convert)
      3. POST /docx/v1/documents/{id}/blocks/{id}/descendant -> insert blocks at the
         document root (children_id = first_level_block_ids, descendants = blocks).

    On a content-write failure (e.g. the convert scope isn't granted) the empty
    titled doc already exists, so we raise an error that includes its URL and the
    likely cause — the caller surfaces it truthfully instead of claiming success.
    """
    data = _req("POST", "/docx/v1/documents", json={"title": title})
    doc_id = data.get("document", {}).get("document_id", "")
    if not doc_id:
        raise RuntimeError("文档创建失败:未返回 document_id。")
    url = f"{LARK_OPEN_BASE.replace('open.', '')}/docx/{doc_id}"

    md = (markdown or "").strip()
    if not md:
        return url
    try:
        conv = _req(
            "POST", "/docx/v1/documents/blocks/convert",
            json={"content_type": "markdown", "content": md},
        )
        blocks = conv.get("blocks") or []
        first_level = conv.get("first_level_block_ids") or []
        if blocks and first_level:
            # children_id / descendants cap at 1000 blocks per request — ample.
            _req(
                "POST", f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant",
                params={"document_revision_id": -1},
                json={"index": 0, "children_id": first_level, "descendants": blocks},
            )
    except Exception as e:
        raise RuntimeError(
            f"文档已创建({url})但正文写入失败:{e}。"
            "若为权限报错,请确认 Lark 应用已开通「docx:document.block:convert」并重新发版。"
        ) from e
    return url


def search_wiki(query: str, page_size: int = 5) -> list[dict]:
    """Search the tenant wiki/knowledge base."""
    data = _req("POST", "/wiki/v1/nodes/search", json={"query": query, "page_size": page_size})
    return data.get("items", []) or []


def query_calendar(start_iso: str, end_iso: str, calendar_id: str = "primary") -> list[dict]:
    """List events in a time window (RFC3339/ISO timestamps)."""
    data = _req(
        "GET", f"/calendar/v4/calendars/{calendar_id}/events",
        params={"start_time": start_iso, "end_time": end_iso},
    )
    return data.get("items", []) or []


# --- File / image delivery (needs scope im:resource or im:resource:upload) ----

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _multipart_post(path: str, data: dict, files: dict) -> dict:
    """POST multipart/form-data with tenant auth (the file-upload endpoints don't
    take JSON, so they bypass _req)."""
    resp = requests.post(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {_tenant_token()}"},
        data=data,
        files=files,
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"Lark API {path} failed: code={body.get('code')} msg={body.get('msg')}")
    return body.get("data", {})


def upload_file(file_path: str, file_name: str | None = None) -> str:
    """Upload a file to Lark (≤30MB). Returns file_key. file_type='stream' works
    for any type; the file_name (with extension) drives how Lark renders it."""
    file_name = file_name or os.path.basename(file_path)
    with open(file_path, "rb") as f:
        data = _multipart_post(
            "/im/v1/files",
            data={"file_type": "stream", "file_name": file_name},
            files={"file": (file_name, f, "application/octet-stream")},
        )
    return data["file_key"]


def upload_image(file_path: str) -> str:
    """Upload an image to Lark (≤10MB). Returns image_key."""
    with open(file_path, "rb") as f:
        data = _multipart_post(
            "/im/v1/images",
            data={"image_type": "message"},
            files={"image": f},
        )
    return data["image_key"]


def send_file_to_chat(chat_id: str, file_path: str) -> None:
    """Upload `file_path` and send it to `chat_id` as a file (or inline image for
    image types). Raises on failure."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _IMAGE_EXTS:
        key = upload_image(file_path)
        content = json.dumps({"image_key": key})
        msg_type = "image"
    else:
        key = upload_file(file_path)
        content = json.dumps({"file_key": key})
        msg_type = "file"
    _req(
        "POST", "/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        json={"receive_id": chat_id, "msg_type": msg_type, "content": content},
    )
