"""Async message handling: parse @-mention, invoke AgentCore Runtime, reply in thread.

Contract with the agent (agent/main.py, Claude Agent SDK on AgentCore Runtime):
  - Lambda invokes the runtime with payload {"chat_id": str, "text": str}.
  - runtimeSessionId is derived deterministically from chat_id so the microVM
    session (and thus short-term context) is stable per channel.
  - The agent yields SSE events; each ``data:`` line is JSON:
      {"type": "text_delta", "text": "..."}   -> accumulate
      {"type": "tool_use",   "name": "..."}   -> note tool (for smoke/debug)
    Plain (non-JSON) data lines are treated as text.
  - The agent itself sets AgentCore Memory actor_id = chat_id (per-channel isolation).
"""

import base64
import json
import logging
import os
import time

import boto3

from lark_api import (
    download_message_resource,
    id_convert_card,
    list_chat_messages,
    mention_markdown,
    reply_card_message,
    reply_text_message,
    send_card_message,
    send_text_message,
    update_card,
)

logger = logging.getLogger(__name__)

AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
MAX_MESSAGE_LEN = 8000
# Bedrock caps a single image at ~5MB; reject larger to give a clear message
# rather than an opaque downstream 400. (Lambda layer has no Pillow to downscale.)
MAX_IMAGE_BYTES = int(4.5 * 1024 * 1024)

# Card update throttle (seconds). Tool-start events bypass this for instant feedback.
CARD_UPDATE_INTERVAL = 0.7
ACK_TEXT = "正在思考…"

# Map agent tool names (often "mcp__<server>__<tool>") to friendly Chinese labels.
# Matched by substring so we don't have to enumerate every MCP tool.
_TOOL_LABELS = [
    ("read_chat_history", "📜 翻看群里的历史消息"),
    ("create_lark_doc", "📝 新建飞书文档"),
    ("search_wiki", "📚 检索知识库"),
    ("query_calendar", "📅 查询日历"),
    ("exa", "🔍 联网搜索"),
    ("web_search", "🔍 联网搜索"),
    ("aws_knowledge", "☁️ 查 AWS 文档"),
    ("aws_pricing", "💰 查 AWS 定价"),
    ("pptx", "📊 制作 PPT"),
    ("docx", "📄 撰写 Word 文档"),
    ("xlsx", "📈 编辑 Excel"),
    ("pdf", "📕 处理 PDF"),
    ("Bash", "⚙️ 执行命令"),
    ("Write", "✍️ 写文件"),
    ("Read", "👀 读取文件"),
]


def _tool_label(name: str) -> str:
    """Friendly label for a tool-in-progress indicator."""
    low = name.lower()
    for key, label in _TOOL_LABELS:
        if key.lower() in low:
            return label
    return f"🔧 {name}"

_agentcore_client = None


def _get_agentcore_client():
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client("bedrock-agentcore")
    return _agentcore_client


def _runtime_session_id(chat_id: str) -> str:
    """AgentCore requires runtimeSessionId length >= 33; derive deterministically."""
    base = f"lark-{chat_id}"
    return base if len(base) >= 33 else base + "0" * (33 - len(base))


# ---------------------------------------------------------------------------
# Ambient-memory enrollment
# ---------------------------------------------------------------------------

CONSOLIDATION_TABLE = os.environ.get("CONSOLIDATION_TABLE", "")
_cursor_tbl = None


def _enroll_chat(chat_id: str) -> None:
    """Register this chat for hourly ambient-memory consolidation on first use.

    Sets the sweep cursor to 'now' only if absent (if_not_exists), so we start
    consolidating chatter from when the bot was first used here — never retro-mine
    older backlog, and never reset progress on later messages. Best-effort: a
    failure here must never affect the reply.
    """
    if not CONSOLIDATION_TABLE or not chat_id:
        return
    global _cursor_tbl
    try:
        if _cursor_tbl is None:
            _cursor_tbl = boto3.resource("dynamodb").Table(CONSOLIDATION_TABLE)
        _cursor_tbl.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression="SET last_ts_ms = if_not_exists(last_ts_ms, :now)",
            ExpressionAttributeValues={":now": int(time.time() * 1000)},
        )
    except Exception:
        logger.warning("enroll_chat failed for %s", chat_id, exc_info=True)


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def _extract(event_data: dict) -> dict | None:
    """Pull the fields we need from an im.message.receive_v1 event.

    Returns None if the message should be ignored (group msg w/o @mention and
    not in a thread the bot is part of).
    """
    message = event_data.get("message", {})
    chat_id = message.get("chat_id", "")
    chat_type = message.get("chat_type", "")  # "group" | "p2p"
    message_id = message.get("message_id", "")
    root_id = message.get("root_id", "")
    mentions = message.get("mentions", []) or []
    msg_type = message.get("message_type", "")
    # Sender's open_id — needed so the agent's schedule_task tool can record WHO to
    # @-mention when a reminder later fires. Lives at event.sender.sender_id.open_id.
    sender_open_id = (
        event_data.get("sender", {}).get("sender_id", {}).get("open_id", "")
    )

    if not chat_id or not message_id:
        return None

    is_group = chat_type == "group"
    # In a group, only act on @mentions or replies within an existing thread.
    if is_group and not mentions and not root_id:
        logger.info("Group msg without mention/thread, skipping")
        return None

    text = _extract_text(message, msg_type, mentions)
    image_keys = _extract_image_keys(message, msg_type)

    if not text and not image_keys:
        # Addressed (passed the mention/thread gate) but nothing we can use — a
        # file/audio/sticker, or a bare @ with no words. Reply gracefully rather
        # than going silent (silence reads as broken).
        logger.info("Addressed but no usable text/image (msg_type=%s)", msg_type)
        return {
            "chat_id": chat_id,
            "message_id": message_id,
            "root_id": root_id,
            "text": "",
            "unsupported": True,
            "sender_open_id": sender_open_id,
        }

    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "root_id": root_id,
        "text": text[:MAX_MESSAGE_LEN],
        "image_keys": image_keys,
        "sender_open_id": sender_open_id,
    }


def _extract_image_keys(message: dict, msg_type: str) -> list[str]:
    """Image keys from an `image` message or images embedded in a `post` message."""
    raw = message.get("content", "")
    try:
        content = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return []
    if msg_type == "image":
        key = content.get("image_key", "")
        return [key] if key else []
    if msg_type == "post":
        keys = []
        for block in content.get("content", []) or []:
            for run in block or []:
                if isinstance(run, dict) and run.get("tag") == "img" and run.get("image_key"):
                    keys.append(run["image_key"])
        return keys
    return []


def _extract_text(message: dict, msg_type: str, mentions: list) -> str:
    """Extract plain text from a text/post message and strip @placeholders."""
    raw = message.get("content", "")
    try:
        content = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        content = {}

    text = ""
    if msg_type == "text":
        text = content.get("text", "")
    elif msg_type == "post":
        # post content: {"title":..., "content": [[{"tag":"text","text":...}, ...], ...]}
        parts = []
        for block in content.get("content", []) or []:
            for run in block or []:
                if isinstance(run, dict) and run.get("tag") == "text":
                    parts.append(run.get("text", ""))
        text = " ".join(parts)
    else:
        # Unsupported type (image/file/etc.) — v1 ignores.
        return ""

    # Strip @-mention placeholders (e.g. "@_user_1")
    for m in mentions:
        key = m.get("key", "")
        if key:
            text = text.replace(key, "")
    return text.strip()


# ---------------------------------------------------------------------------
# AgentCore Runtime invocation
# ---------------------------------------------------------------------------

def invoke_agent_streaming(
    session_id: str,
    text: str,
    chat_id: str = "",
    images: list[dict] | None = None,
    sender_open_id: str = "",
):
    """Invoke AgentCore Runtime and yield (accumulated_text, tool_name) tuples.

    tool_name is non-empty only on the event where a tool call starts.
    images (optional): [{"data": <base64 str>, "media_type": "image/png"}, ...] for
    multimodal input — forwarded to the agent which builds image content blocks.
    sender_open_id (optional): forwarded so the agent's schedule_task tool can
    record who to @-mention when a reminder fires.
    """
    if not AGENT_RUNTIME_ARN:
        raise RuntimeError("AGENT_RUNTIME_ARN not configured")

    body = {"chat_id": chat_id or session_id, "text": text}
    if images:
        body["images"] = images
    if sender_open_id:
        body["sender_open_id"] = sender_open_id
    payload = json.dumps(body).encode("utf-8")
    resp = _get_agentcore_client().invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        runtimeSessionId=_runtime_session_id(session_id),
        payload=payload,
        qualifier="DEFAULT",
    )

    accumulated = ""
    for line in resp["response"].iter_lines(chunk_size=1):
        if not line:
            continue
        decoded = line.decode("utf-8") if isinstance(line, bytes) else line
        if not decoded.startswith("data:"):
            continue
        data = decoded[len("data:"):].strip()
        if not data:
            continue
        tool_name = ""
        try:
            obj = json.loads(data)
            if isinstance(obj, dict):
                if obj.get("type") == "text_delta":
                    accumulated += obj.get("text", "")
                elif obj.get("type") == "tool_use":
                    tool_name = obj.get("name", "")
                elif "data" in obj:  # tolerate Strands-style {"data": "..."}
                    accumulated += obj.get("data", "")
            elif isinstance(obj, str):
                accumulated += obj
        except json.JSONDecodeError:
            accumulated += data
        yield accumulated, tool_name


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def handle_message_async(event_data: dict):
    """Handle an @-mention with a streaming card so the user gets an instant ack.

    Flow:
      1. Reply with a card ("正在思考…") in the triggering message's thread → message_id
      2. id_convert(message_id) → card_id
      3. Stream from the agent, updating the card in place (~0.7s throttle); a
         tool call shows "🔧 <tool>" immediately so progress is visible
      4. Final card update with the clean reply

    Falls back to a plain text reply if the card path fails at any point.
    """
    parsed = _extract(event_data)
    if not parsed:
        return

    chat_id = parsed["chat_id"]
    message_id = parsed["message_id"]
    text = parsed["text"]
    sender_open_id = parsed.get("sender_open_id", "")

    # First time the bot is used in this chat, enroll it for ambient consolidation.
    _enroll_chat(chat_id)

    # Non-text/non-image message addressed at the bot — reply gracefully, don't invoke.
    if parsed.get("unsupported"):
        try:
            reply_text_message(message_id, "我现在能看文字和图片~文件、语音这些还处理不了。把要点用文字发我就行 🙂")
        except Exception:
            logger.exception("unsupported-message reply failed")
        return

    # --- Download any attached images → base64 for multimodal input ---
    images = []
    for key in (parsed.get("image_keys") or [])[:4]:  # cap count per message
        try:
            data, media_type = download_message_resource(message_id, key, "image")
        except Exception:
            logger.exception("image download failed: %s", key)
            continue
        if len(data) > MAX_IMAGE_BYTES:
            try:
                reply_text_message(message_id, "这张图有点大(>4.5MB),麻烦压缩一下再发我 🙂")
            except Exception:
                logger.exception("oversize reply failed")
            return
        images.append({"data": base64.b64encode(data).decode("ascii"), "media_type": media_type})
        logger.info("image attached: bytes=%d type=%s", len(data), media_type)
    if (parsed.get("image_keys")) and not images:
        try:
            reply_text_message(message_id, "图片好像没下载下来,稍后再试或换张图发我?")
        except Exception:
            logger.exception("image-fail reply failed")
        return
    if images and not text:
        text = "看看这张图,帮我分析一下。"

    # --- Instant ack: reply with a placeholder card, then resolve its card_id ---
    card_id = None
    ack_message_id = reply_card_message(message_id, ACK_TEXT)
    if ack_message_id:
        card_id = id_convert_card(ack_message_id)
        if not card_id:
            logger.warning("id_convert failed; will fall back to text reply")
    else:
        logger.warning("card ack failed; will fall back to text reply")

    # --- Streaming path: progressive card updates ---
    if card_id:
        sequence = 1
        last_update = 0.0
        final_text = ""
        try:
            for accumulated_text, tool_name in invoke_agent_streaming(
                chat_id, text, chat_id=chat_id, images=images, sender_open_id=sender_open_id
            ):
                final_text = accumulated_text
                now = time.time()
                # Tool starts update immediately; text deltas are throttled.
                if tool_name or (now - last_update >= CARD_UPDATE_INTERVAL):
                    if tool_name:
                        body = (accumulated_text or ACK_TEXT) + f"\n\n---\n{_tool_label(tool_name)}…"
                    else:
                        body = accumulated_text or ACK_TEXT
                    update_card(card_id, body, sequence)
                    sequence += 1
                    last_update = now
        except Exception:
            logger.exception("Agent streaming failed")
            if not final_text:
                final_text = "抱歉,我现在遇到点问题,稍后再试一下。"
        # Final clean update (no tool indicator).
        update_card(card_id, final_text or "(没有生成回复)", sequence)
        logger.info("Streaming complete: chat=%s updates=%d reply_len=%d", chat_id, sequence, len(final_text))
        return

    # --- Fallback path: non-streaming text reply ---
    reply = ""
    try:
        for accumulated_text, _tool in invoke_agent_streaming(
            chat_id, text, chat_id=chat_id, images=images, sender_open_id=sender_open_id
        ):
            reply = accumulated_text
    except Exception:
        logger.exception("Agent invocation failed")
        reply = "抱歉,我现在遇到点问题,稍后再试一下。"
    if not reply:
        reply = "(没有生成回复)"
    try:
        reply_text_message(message_id, reply)
    except Exception:
        logger.exception("reply failed, falling back to send")
        try:
            send_text_message(chat_id, reply)
        except Exception:
            logger.exception("send fallback also failed")


def handle_scheduled_task(
    chat_id: str, mode: str, payload: str, mention_open_id: str = "", title: str = ""
):
    """Deliver a fired scheduled job to a chat.

    Unlike handle_message_async there is no triggering message, so we SEND a new
    card (not reply). `mention_open_id` (the job's creator) is @-mentioned via the
    verified card-markdown syntax so "提醒我" actually pings them.

      mode == "remind": post the stored text directly (no agent — cheap/instant).
      mode == "agent":  run an agent turn with the stored prompt, streamed into a
                        card just like a normal @-mention.
    """
    if not chat_id or not payload:
        logger.warning("scheduled task missing chat_id/payload; skipping")
        return
    at = mention_markdown(mention_open_id)
    card_title = title or ("提醒" if mode == "remind" else "Claude")

    # --- remind mode: one card, no agent ---
    if mode == "remind":
        try:
            send_card_message(chat_id, at + payload, title=card_title)
        except Exception:
            logger.exception("remind send failed; trying plain text")
            try:
                send_text_message(chat_id, payload)
            except Exception:
                logger.exception("remind text fallback also failed")
        return

    # --- agent mode: ack card → stream the agent turn → update in place ---
    card_id = None
    ack_message_id = send_card_message(chat_id, ACK_TEXT, title=card_title)
    if ack_message_id:
        card_id = id_convert_card(ack_message_id)

    if not card_id:
        # No streamable card — run once and send the result as a single card.
        reply = ""
        try:
            for accumulated_text, _tool in invoke_agent_streaming(chat_id, payload, chat_id=chat_id):
                reply = accumulated_text
        except Exception:
            logger.exception("scheduled agent run failed")
            reply = "定时任务执行出错了,稍后我再试。"
        try:
            send_card_message(chat_id, at + (reply or "(没有生成内容)"), title=card_title)
        except Exception:
            logger.exception("scheduled fallback send failed")
        return

    sequence = 1
    last_update = 0.0
    final_text = ""
    try:
        for accumulated_text, tool_name in invoke_agent_streaming(chat_id, payload, chat_id=chat_id):
            final_text = accumulated_text
            now = time.time()
            if tool_name or (now - last_update >= CARD_UPDATE_INTERVAL):
                body = accumulated_text or ACK_TEXT
                if tool_name:
                    body += f"\n\n---\n{_tool_label(tool_name)}…"
                update_card(card_id, at + body, sequence)
                sequence += 1
                last_update = now
    except Exception:
        logger.exception("scheduled agent streaming failed")
        if not final_text:
            final_text = "定时任务执行出错了,稍后我再试。"
    update_card(card_id, at + (final_text or "(没有生成内容)"), sequence)
