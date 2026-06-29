"""AgentCore Runtime entrypoint — Claude Agent SDK agent for the Lark teammate.

Backend: LiteLLM gateway (Anthropic /v1/messages format) -> Bedrock provider.
  ANTHROPIC_BASE_URL = LiteLLM ALB, ANTHROPIC_API_KEY = LiteLLM key,
  model = the alias configured in the gateway (LITELLM_MODEL).

Key gotcha: the Claude Agent SDK spawns the `claude` CLI as a subprocess; the
SDK's *bundled* CLI binary ignores ANTHROPIC_BASE_URL, so we force the
system-installed CLI via cli_path. (See known issue anthropics/claude-agent-sdk-python#677.)

Tools/skills are all client-side (survive Bedrock pseudo-passthrough): in-process
Lark SDK MCP server + external MCP (Exa / AWS Knowledge / pricing) + Claude Code
skills loaded from ./.claude/skills. No built-in server tools, no betas.

Latency design — WARM CLIENT REUSE:
  Spawning the `claude` CLI subprocess and (re)connecting all MCP servers costs
  ~8-10s. AgentCore microVMs are session-isolated and serve requests serially,
  so we connect ONE module-level ClaudeSDKClient on first use and reuse it across
  invocations (CLI + MCP stay warm). To keep turns isolated (no cross-chat context
  bleed) we issue each turn under a fresh `session_id`, and the per-turn long-term
  memory is injected into the *prompt* (not the system prompt, which is static and
  baked into the warm client). A dead/broken client is torn down and rebuilt on the
  next request.

Output contract (consumed by functions/lark-webhook/event_handlers.py):
  yields {"type": "text_delta", "text": ...} and {"type": "tool_use", "name": ...}
"""

import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

import runtime_config  # noqa: F401 — loads config/secrets from Secrets Manager on import (before `import memory`)

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

import memory
from mcp_config import build_external_mcp
from prompts import SYSTEM_PROMPT
from tools import (
    LARK_ALLOWED,
    build_lark_mcp_server,
    clear_skills_dirty,
    set_current_chat,
    set_current_user,
    skills_dirty,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

MODEL = os.environ.get("LITELLM_MODEL", "claude-opus-4-8")
logger.info("LiteLLM model in use: %s", MODEL)
APP_DIR = os.environ.get("AGENT_APP_DIR", "/app")
# Skill workflows (e.g. html2pptx: write HTML, run scripts, render, deliver) take
# many tool turns — 20 was far too low and cut PPT generation off mid-way.
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "80"))

# System CLI path (NOT the SDK's bundled binary — see module docstring).
_CLI_PATH = shutil.which("claude")


def _build_options() -> ClaudeAgentOptions:
    """Static options for the warm client. No per-turn memory here — the system
    prompt must stay constant so a single connected client can be reused; per-turn
    long-term memory is prepended to the query prompt instead."""
    lark_server = build_lark_mcp_server()
    ext_servers, ext_allowed = build_external_mcp()

    kwargs = dict(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"lark": lark_server, **ext_servers},
        allowed_tools=LARK_ALLOWED + ext_allowed,
        max_turns=MAX_TURNS,
        cwd=APP_DIR,                 # Claude Code skills load from ./.claude/skills
        setting_sources=["project"], # enable project-level settings + skills
        include_partial_messages=True,  # emit StreamEvent deltas → token-level streaming to the card
        # Headless server agent in an isolated microVM: skills must run Bash/Write/Read
        # without interactive approval (there's no human to approve in a Lark group).
        permission_mode="bypassPermissions",
    )
    if _CLI_PATH:
        kwargs["cli_path"] = _CLI_PATH
    # Ensure the subprocess CLI sees the LiteLLM endpoint even if the parent env
    # is partially set; ClaudeAgentOptions.env is forwarded to the CLI process.
    kwargs["env"] = {
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    }
    return ClaudeAgentOptions(**kwargs)


# --- Warm client lifecycle ---------------------------------------------------
# One persistent ClaudeSDKClient, connected lazily and reused across invocations.
#
# IMPORTANT — what `session_id` on query() does NOT do: in claude-agent-sdk 0.2.108
# the session_id is just a field on the stdin message; it does NOT fork a fresh
# transcript or reset context. A single connect() == one long-lived CLI process
# whose turns all accumulate into one conversation. So:
#   * Within one chat that accumulation IS the short-term memory (multi-turn follow-
#     ups work for free while the container stays warm).
#   * AgentCore routes each runtimeSessionId (= chat_id) to its own isolated microVM,
#     so different chats normally land on different containers. As defense-in-depth
#     against a microVM ever being reused across chats, we bind the warm client to a
#     chat_id and REBUILD it if a different chat_id arrives (guarantees no cross-chat
#     context bleed). We also rebuild after MAX_WARM_TURNS to bound context growth.
# `_client_lock` serializes turns (AgentCore is already single-request-serial; the
# lock guards the rebuild path and the shared receive stream).
MAX_WARM_TURNS = int(os.environ.get("AGENT_MAX_WARM_TURNS", "25"))

_client: ClaudeSDKClient | None = None
_client_chat_id: str | None = None
_client_turns = 0
_client_lock = asyncio.Lock()


async def _get_client(chat_id: str) -> tuple[ClaudeSDKClient, bool]:
    """Return (warm client bound to `chat_id`, freshly_built). Rebuilds on chat
    change, turn cap, or a pending skill change (so the CLI re-discovers skills)."""
    global _client, _client_chat_id, _client_turns
    skills_changed = skills_dirty()
    if _client is not None and (
        _client_chat_id != chat_id or _client_turns >= MAX_WARM_TURNS or skills_changed
    ):
        if skills_changed:
            reason = "skills changed"
        elif _client_chat_id != chat_id:
            reason = "chat changed"
        else:
            reason = f"turn cap {MAX_WARM_TURNS}"
        logger.info("[warm] rebuilding client (%s)", reason)
        await _reset_client()
    fresh = False
    if _client is None:
        clear_skills_dirty()  # the rebuild below will load the latest skills
        client = ClaudeSDKClient(options=_build_options())
        await client.connect()  # spawns CLI + connects all MCP servers ONCE
        _client = client
        _client_chat_id = chat_id
        _client_turns = 0
        fresh = True
        logger.info("[warm] ClaudeSDKClient connected (CLI + MCP warm) for chat=%s", chat_id)
    _client_turns += 1
    return _client, fresh


async def _reset_client() -> None:
    """Tear down a dead/broken client so the next request rebuilds it."""
    global _client, _client_chat_id, _client_turns
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:  # noqa: BLE001 — best-effort teardown
            logger.warning("[warm] disconnect during reset failed (ignored)", exc_info=True)
    _client = None
    _client_chat_id = None
    _client_turns = 0


# Last user message per chat — used to expand a vague follow-up ("继续"/"你觉得呢")
# into a richer retrieval query so memory recall doesn't collapse on short turns.
_last_text: dict[str, str] = {}


def _retrieval_query(chat_id: str, text: str) -> str:
    """Expand a short/vague message with the previous turn for better recall."""
    prev = _last_text.get(chat_id, "")
    if prev and len(text) < 12:
        return f"{prev} {text}".strip()
    return text


def _looks_like_followup(text: str) -> bool:
    """Heuristic: is this a short/referential message that only makes sense with
    the recent conversation (so re-seeding raw turns helps)? A self-contained
    instruction (e.g. a full scheduling request) is NOT — re-seeding raw turns
    into it only risks bleeding an unrelated recent task into the parse. Length is
    a robust proxy: follow-ups ("继续"/"那个再改下") are short; commands are longer."""
    return len(text.strip()) < 15


# Container clock is UTC; the team's working timezone is Asia/Shanghai (UTC+8, no
# DST). Computed by offset to avoid a tzdata dependency in the image.
def _now_line() -> str:
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    return f"[当前时间] {now.strftime('%Y-%m-%d %H:%M:%S')} Asia/Shanghai (UTC+8)"


def _build_prompt(text: str, mem_snippets: list[str], recent: list[str] | None = None) -> str:
    # Always give the agent the current time so it can turn "过 10 分钟 / 明天 9 点"
    # into the relative seconds that schedule_task expects.
    parts: list[str] = [_now_line()]
    has_bg = False
    if recent:
        # Background only — clearly NOT the current task. Truncated so a verbose
        # past turn (e.g. a prior news dump) can't bleed into the current request.
        trimmed = [(ln[:80] + "…") if len(ln) > 80 else ln for ln in recent]
        parts.append("[背景·最近聊过的(仅供判断是否在接着聊,不是当前任务)]\n" + "\n".join(trimmed))
        has_bg = True
    if mem_snippets:
        parts.append("[背景·这个群的长期记忆(相关片段,供参考)]\n" + "\n".join(f"- {s}" for s in mem_snippets))
        has_bg = True
    parts.append(f"[用户消息(你要回应/执行的就是这一条)]\n{text}")
    if has_bg:
        # Hard delineation: stop background context from being treated as the task.
        parts.append(
            "提示:上面带[背景]的内容只是参考,你要回应和执行的只有[用户消息]这一条。"
            "尤其是定时/提醒这类任务,频率、动作、持续时间只能取[用户消息]的字面意思,"
            "绝不要把背景里提到的其他任务或话题混进来。"
        )
    return "\n\n".join(parts)


async def _multimodal_message(text: str, images: list[dict], session_id: str):
    """Yield a single streaming-input user message with text + base64 image blocks.
    LiteLLM /v1/messages -> Bedrock only accepts base64 image sources (not url)."""
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for im in images:
        data = im.get("data")
        if not data:
            continue
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": im.get("media_type", "image/png"),
                "data": data,
            },
        })
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


@app.entrypoint
async def agent_invocation(payload, context):
    chat_id = payload.get("chat_id", "unknown")
    # Lark webhook sends {chat_id, text}; `agentcore invoke` sends {prompt}. Accept both.
    text = payload.get("text") or payload.get("prompt") or ""
    session_id = payload.get("session_id", chat_id)
    images = payload.get("images") or []
    sender_open_id = payload.get("sender_open_id", "")
    logger.info("[req] chat_id=%s text_len=%d images=%d", chat_id, len(text), len(images))
    set_current_chat(chat_id)  # so deliver_file knows which chat to post to
    set_current_user(sender_open_id)  # so schedule_task knows who to @ on reminders

    mem_snippets = memory.retrieve(chat_id, _retrieval_query(chat_id, text), session_id=session_id)
    if mem_snippets:
        logger.info("Loaded %d memory snippets", len(mem_snippets))

    assistant_text = ""
    saw_stream_event = False  # if partial StreamEvents arrive, they are the source of
    # truth — skip AssistantMessage entirely (it would re-emit the same text/tools).
    async with _client_lock:
        try:
            # Warm client is bound to chat_id; rebuilt on chat change (isolation),
            # turn cap (bounded growth), or skill change. Within a chat it carries
            # short-term context; on a fresh build we re-seed it from recent events.
            client, fresh = await _get_client(chat_id)
            # Re-seed raw recent turns only on a fresh client AND only for short
            # follow-ups that need them — a self-contained instruction gets a clean
            # prompt (no raw-turn bleed). Long-term memory (relevance-filtered) still
            # carries durable context either way.
            recent = (
                memory.recent_events(chat_id, session_id, n=6)
                if (fresh and _looks_like_followup(text))
                else None
            )
            if recent:
                logger.info("Re-seeded %d recent turns into fresh client", len(recent))
            prompt = _build_prompt(text, mem_snippets, recent)
            if images:
                await client.query(_multimodal_message(prompt, images, session_id), session_id=session_id)
            else:
                await client.query(prompt, session_id=session_id)
            async for msg in client.receive_response():
                if isinstance(msg, StreamEvent):
                    saw_stream_event = True
                    # Only stream the top-level agent turn (skip nested subagent/skill streams).
                    if msg.parent_tool_use_id:
                        continue
                    ev = msg.event or {}
                    etype = ev.get("type")
                    if etype == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text", "")
                            if chunk:
                                assistant_text += chunk
                                yield {"type": "text_delta", "text": chunk}
                    elif etype == "content_block_start":
                        cb = ev.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            yield {"type": "tool_use", "name": cb.get("name", "")}
                elif isinstance(msg, AssistantMessage):
                    # Fallback only when token streaming didn't happen at all (e.g. the
                    # gateway buffered the response). Otherwise StreamEvents already did it.
                    if saw_stream_event:
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            assistant_text += block.text
                            yield {"type": "text_delta", "text": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {"type": "tool_use", "name": block.name}
        except Exception:  # noqa: BLE001 — turn failed; drop the warm client so the
            # next request reconnects cleanly. Webhook side renders a fallback reply.
            # warning (not error/exception level): the failure is handled here — we
            # reset the warm client and re-raise so the webhook surfaces a fallback
            # reply — so this isn't an unhandled, swallowed error. exc_info keeps the
            # traceback for debugging.
            logger.warning("agent turn failed; resetting warm client", exc_info=True)
            await _reset_client()
            raise

    # Persist the turn; long-term extraction runs async in AgentCore Memory.
    memory.record_turn(chat_id, session_id, text, assistant_text)
    if text:
        _last_text[chat_id] = text  # for next-turn vague-follow-up query expansion
    logger.info("[done] chat_id=%s reply_len=%d", chat_id, len(assistant_text))


if __name__ == "__main__":
    app.run()
