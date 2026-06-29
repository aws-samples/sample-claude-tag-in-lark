"""AgentCore Memory integration — MANUAL (Claude Agent SDK has no turnkey
session manager like Strands does).

Per-channel isolation: actor_id = Lark chat_id.

This module gives the agent active control over its own memory, not just passive
extraction:

  Recall (before query):
    - retrieve(): semantic search over BOTH the facts namespace (SEMANTIC strategy)
      and the per-session summary namespace (SUMMARIZATION strategy), merged. A
      ListMemoryRecords fallback surfaces freshly-written explicit facts that the
      semantic index may not have picked up yet.
    - recent_events(): raw recent turns via ListEvents — used to re-seed short-term
      context after a warm-client rebuild / cold start (the warm CLI process is
      ephemeral; this recovers "what we were just talking about").

  Write:
    - remember_fact(): DIRECT write via BatchCreateMemoryRecords → immediately
      retrievable, bypassing the minutes-delayed async extraction. This is the fix
      for "I told it just now but it forgot".
    - record_turn(): unchanged passive path — persist the raw turn as an event so
      AgentCore's async extraction keeps building long-term memory in the background.

  Curate (forgetting / superseding):
    - forget_fact(): find the best-matching fact and DeleteMemoryRecord it.

Everything is wrapped defensively: any API mismatch degrades that operation to a
no-op (logged) rather than crashing the agent turn.
"""

import logging
import os
import time
import uuid

import boto3

logger = logging.getLogger(__name__)

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# Namespace templates — must match what create_memory.py configured on each strategy.
FACTS_NS_TMPL = os.environ.get("MEMORY_NAMESPACE_TMPL", "/actor/{actorId}/facts")
SUMMARY_NS_TMPL = os.environ.get(
    "MEMORY_SUMMARY_NS_TMPL", "/actor/{actorId}/session/{sessionId}/summary"
)
# Strategy id of the SEMANTIC ("channel_facts") strategy. Required so a directly
# written record lands in the same retrievable index. Read from GetMemory at deploy
# and injected via the runtime secret. If empty, remember_fact still writes but the
# record may not associate with the semantic index (ListMemoryRecords still finds it).
SEMANTIC_STRATEGY_ID = os.environ.get("MEMORY_SEMANTIC_STRATEGY_ID", "")

# Optional escape hatch: also scan recent records in the facts namespace and merge
# them into recall. Default OFF — testing showed a directly-written record takes
# ~20-65s to be indexed (so list is unreliable sub-30s AND adds a per-turn call),
# while the real "just told it" immediacy is covered by recent_events() re-seeding
# raw turns from ListEvents (which IS instant, ~0.25s). Flip to "1" if recall gaps
# appear for durable facts that semantic search ranks below top_k.
LIST_FALLBACK = os.environ.get("MEMORY_LIST_FALLBACK", "0") == "1"

_client = None


def _c():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-agentcore", region_name=REGION)
    return _client


def enabled() -> bool:
    return bool(MEMORY_ID)


def _rec_text(rec: dict) -> str:
    content = rec.get("content", {})
    if isinstance(content, dict):
        return content.get("text") or ""
    return ""


def _rec_id(rec: dict) -> str:
    return rec.get("memoryRecordId") or rec.get("id") or ""


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def retrieve(actor_id: str, query: str, session_id: str | None = None, top_k: int = 5) -> list[str]:
    """Return relevant long-term memory snippets for this channel, or [].

    Searches the facts namespace and (if session_id given) the session-summary
    namespace, then merges with a recent-records fallback. De-duplicated by text,
    capped at top_k+2.
    """
    if not MEMORY_ID or not query:
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _add(text: str):
        t = (text or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    # 1) Semantic search over facts.
    facts_ns = FACTS_NS_TMPL.format(actorId=actor_id)
    try:
        resp = _c().retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=facts_ns,
            searchCriteria={"searchQuery": query, "topK": top_k},
        )
        for rec in resp.get("memoryRecordSummaries", []) or []:
            _add(_rec_text(rec))
    except Exception:
        logger.warning("retrieve(facts) failed", exc_info=True)

    # 2) Semantic search over the session summary (if we know the session).
    if session_id:
        summary_ns = SUMMARY_NS_TMPL.format(actorId=actor_id, sessionId=session_id)
        try:
            resp = _c().retrieve_memory_records(
                memoryId=MEMORY_ID,
                namespace=summary_ns,
                searchCriteria={"searchQuery": query, "topK": 2},
            )
            for rec in resp.get("memoryRecordSummaries", []) or []:
                _add(_rec_text(rec))
        except Exception:
            logger.warning("retrieve(summary) failed", exc_info=True)

    # 3) Fallback: most-recent explicit facts (in case the semantic index lags).
    if LIST_FALLBACK:
        for rec in _list_recent_facts(actor_id, limit=10):
            _add(_rec_text(rec))
            if len(out) >= top_k + 2:
                break

    return out[: top_k + 2]


def _list_recent_facts(actor_id: str, limit: int = 10) -> list[dict]:
    """Recent records in the facts namespace, newest first (best-effort)."""
    facts_ns = FACTS_NS_TMPL.format(actorId=actor_id)
    try:
        resp = _c().list_memory_records(
            memoryId=MEMORY_ID, namespace=facts_ns, maxResults=limit
        )
        recs = resp.get("memoryRecordSummaries") or resp.get("memoryRecords") or []
        # Sort newest-first when a timestamp-ish field is present.
        def _ts(r):
            return r.get("createdAt") or r.get("timestamp") or r.get("memoryRecordCreatedAt") or 0
        try:
            recs = sorted(recs, key=_ts, reverse=True)
        except Exception:  # nosec B110 — best-effort sort; unsorted is acceptable, never fatal
            pass
        return recs[:limit]
    except Exception:
        logger.warning("list_memory_records fallback failed", exc_info=True)
        return []


def recent_events(actor_id: str, session_id: str, n: int = 6) -> list[str]:
    """Raw recent turns as 'ROLE: text' lines (oldest→newest), for re-seeding
    short-term context after a warm-client rebuild / cold start. Best-effort."""
    if not MEMORY_ID or not session_id:
        return []
    try:
        resp = _c().list_events(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            includePayloads=True,
            maxResults=max(1, min(n, 20)),
        )
    except Exception:
        logger.warning("recent_events(list_events) failed", exc_info=True)
        return []
    lines: list[str] = []
    for ev in resp.get("events", []) or []:
        for item in ev.get("payload", []) or []:
            conv = item.get("conversational") if isinstance(item, dict) else None
            if not conv:
                continue
            role = conv.get("role", "")
            text = (conv.get("content", {}) or {}).get("text", "")
            if text:
                lines.append(f"{role}: {text}")
    # list_events returns newest-first by default; present oldest→newest for reading.
    lines.reverse()
    return lines[-n:]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def remember_fact(actor_id: str, text: str) -> bool:
    """Directly persist a fact that is IMMEDIATELY retrievable (no async wait).

    Used by the `remember` tool. Returns True on success.
    """
    text = (text or "").strip()
    if not MEMORY_ID or not text:
        return False
    facts_ns = FACTS_NS_TMPL.format(actorId=actor_id)
    record = {
        "requestIdentifier": f"{actor_id}-{uuid.uuid4().hex}",
        "namespaces": [facts_ns],
        "content": {"text": text},
        "timestamp": int(time.time()),
    }
    if SEMANTIC_STRATEGY_ID:
        record["memoryStrategyId"] = SEMANTIC_STRATEGY_ID
    try:
        _c().batch_create_memory_records(memoryId=MEMORY_ID, records=[record])
        return True
    except Exception:
        logger.warning("remember_fact failed", exc_info=True)
        return False


def record_turn(actor_id: str, session_id: str, user_text: str, assistant_text: str) -> None:
    """Persist this turn as a short-term event; LTM extraction runs async."""
    if not MEMORY_ID:
        return
    turns = []
    if user_text and user_text.strip():
        turns.append({"conversational": {"role": "USER", "content": {"text": user_text}}})
    if assistant_text and assistant_text.strip():
        turns.append({"conversational": {"role": "ASSISTANT", "content": {"text": assistant_text}}})
    if not turns:
        return
    try:
        _c().create_event(
            memoryId=MEMORY_ID,
            actorId=actor_id,
            sessionId=session_id,
            eventTimestamp=int(time.time()),
            payload=turns,
        )
    except Exception:
        logger.warning("memory.record_turn failed (turn not persisted)", exc_info=True)


# ---------------------------------------------------------------------------
# Curate (forget / supersede)
# ---------------------------------------------------------------------------

def forget_fact(actor_id: str, query: str) -> str:
    """Find the fact best matching `query` and delete it. Returns the deleted
    text (so the caller can confirm), or "" if nothing matched / on failure."""
    if not MEMORY_ID or not query:
        return ""
    facts_ns = FACTS_NS_TMPL.format(actorId=actor_id)
    try:
        resp = _c().retrieve_memory_records(
            memoryId=MEMORY_ID,
            namespace=facts_ns,
            searchCriteria={"searchQuery": query, "topK": 1},
        )
        recs = resp.get("memoryRecordSummaries", []) or []
        if not recs:
            return ""
        rid = _rec_id(recs[0])
        text = _rec_text(recs[0])
        if not rid:
            return ""
        _c().delete_memory_record(memoryId=MEMORY_ID, memoryRecordId=rid)
        return text or "(已删除)"
    except Exception:
        logger.warning("forget_fact failed", exc_info=True)
        return ""
