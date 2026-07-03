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

  Curate (forgetting / superseding) — TWO-PHASE, never blind-delete:
    - find_facts(): semantic search returning candidates (id + text + score),
      explicit layer first. Phase 1 of forgetting: the agent (and the user) see
      exactly what would be deleted before anything is deleted.
    - delete_record(): delete ONE record by its exact id. Phase 2.
    (An earlier design deleted the top-1 semantic match directly; with a memory
    pool polluted by many similar records, that repeatedly deleted innocent
    neighbors — e.g. a query containing "CMK" landing on KMS troubleshooting
    memories. Deletion is now only ever by confirmed record id.)

  LAYERING — explicit facts live in their own namespace, deliberately NOT
  associated with any strategy:
    - Explicit "the user told me to remember this" facts go to /actor/{id}/explicit.
      Records without a strategy association are still semantically searchable
      (verified) but sit OUTSIDE the SEMANTIC strategy's consolidation domain, so
      the service's background consolidation cannot silently rewrite or retire
      them — only an explicit delete_record() can. (Strategy-extracted records in
      /facts HAVE been observed to be merged/retired by consolidation.)
    - Auto-extracted facts (async extraction of turns, ambient 旁听) stay in
      /actor/{id}/facts; they are lower-trust background knowledge.

Everything is wrapped defensively: any API mismatch degrades that operation to a
no-op (logged) rather than crashing the agent turn.
"""

import difflib
import logging
import os
import re
import time
import uuid

import boto3

logger = logging.getLogger(__name__)

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# Namespace templates — FACTS/SUMMARY must match what create_memory.py configured
# on each strategy. EXPLICIT is strategy-free by design (see module docstring).
FACTS_NS_TMPL = os.environ.get("MEMORY_NAMESPACE_TMPL", "/actor/{actorId}/facts")
EXPLICIT_NS_TMPL = os.environ.get("MEMORY_EXPLICIT_NS_TMPL", "/actor/{actorId}/explicit")
SUMMARY_NS_TMPL = os.environ.get(
    "MEMORY_SUMMARY_NS_TMPL", "/actor/{actorId}/session/{sessionId}/summary"
)
# NOTE: explicit records are deliberately written WITHOUT a memoryStrategyId
# (MEMORY_SEMANTIC_STRATEGY_ID is no longer consumed here) — strategy-free
# records are still semantically searchable but immune to the strategy's
# background consolidation. See module docstring.

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


def _rec_epoch(rec: dict) -> float:
    """createdAt as epoch seconds (0 if absent) — boto3 returns a datetime."""
    ts = rec.get("createdAt") or rec.get("timestamp")
    try:
        return ts.timestamp()
    except AttributeError:
        try:
            return float(ts or 0)
        except (TypeError, ValueError):
            return 0.0


# Source-tag prefixes ("[群聊旁听] ", "[个人动态] "…) are provenance, not content —
# strip them before comparing, or the same fact tagged two ways never dedupes.
_TAG_RE = re.compile(r"^\[[^\]]{1,12}\]\s*")
_NOISE_RE = re.compile(r"[\s,,.。、;;::!!??*~·'\"“”‘’()()]+")


def _norm_text(t: str) -> str:
    return _NOISE_RE.sub("", _TAG_RE.sub("", (t or "").strip())).lower()


def _is_dup(a: str, b: str) -> bool:
    """Fuzzy same-fact check: containment or high sequence similarity after
    normalization. The explicit tool and async extraction routinely double-write
    the same fact with slightly different wording."""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.85


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def retrieve(actor_id: str, query: str, session_id: str | None = None, top_k: int = 5) -> list[str]:
    """Return relevant long-term memory snippets for this channel, or [].

    Layered: explicit facts (user-dictated, highest trust) first, then
    auto-extracted facts, then (if session_id given) the CURRENT day's session
    summary (session_id rotates daily in main.py, so stale summaries age out of
    recall instead of replaying superseded facts forever).

    Within a layer, candidates are ordered by semantic-score bucket (0.1 wide)
    with newer records winning inside a bucket — a stale fact must out-SCORE a
    fresh one to outrank it, near-ties go to recency. Merged with fuzzy
    de-duplication (higher-trust copy wins), capped at top_k+2.
    """
    if not MEMORY_ID or not query:
        return []

    cands: list[dict] = []

    def _search(namespace: str, layer: int, k: int, label: str):
        try:
            resp = _c().retrieve_memory_records(
                memoryId=MEMORY_ID,
                namespace=namespace,
                searchCriteria={"searchQuery": query, "topK": k},
            )
            for rec in resp.get("memoryRecordSummaries", []) or []:
                text = _rec_text(rec).strip()
                if text:
                    cands.append({
                        "text": text,
                        "layer": layer,
                        "score": float(rec.get("score") or 0),
                        "epoch": _rec_epoch(rec),
                    })
        except Exception:
            logger.warning("retrieve(%s) failed", label, exc_info=True)

    _search(EXPLICIT_NS_TMPL.format(actorId=actor_id), 0, top_k, "explicit")
    _search(FACTS_NS_TMPL.format(actorId=actor_id), 1, top_k, "facts")
    if session_id:
        _search(SUMMARY_NS_TMPL.format(actorId=actor_id, sessionId=session_id), 2, 2, "summary")

    # Optional escape hatch: recent explicit facts in case the semantic index lags.
    if LIST_FALLBACK:
        for rec in _list_recent_facts(actor_id, limit=10):
            text = _rec_text(rec).strip()
            if text:
                cands.append({"text": text, "layer": 1, "score": 0.0, "epoch": _rec_epoch(rec)})

    cands.sort(key=lambda c: (c["layer"], -round(c["score"] * 10), -c["epoch"]))
    out: list[str] = []
    for c in cands:
        if any(_is_dup(c["text"], kept) for kept in out):
            continue
        out.append(c["text"])
        if len(out) >= top_k + 2:
            break
    return out


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


def list_facts(actor_id: str, explicit_limit: int = 50, auto_limit: int = 20) -> dict:
    """Full memory inventory for the transparency tool ("what do you remember?"):
    ALL explicit facts plus the most recent auto-extracted ones, newest first.
    Semantic search can only ever show what matches a query; this shows everything.
    Returns {"explicit": [{"id","text","epoch"}...], "auto": [...]}, best-effort."""

    def _list(namespace: str, limit: int) -> list[dict]:
        try:
            resp = _c().list_memory_records(
                memoryId=MEMORY_ID, namespace=namespace, maxResults=min(limit, 100)
            )
            recs = resp.get("memoryRecordSummaries") or resp.get("memoryRecords") or []
            items = [
                {"id": _rec_id(r), "text": _rec_text(r).strip(), "epoch": _rec_epoch(r)}
                for r in recs
                if _rec_text(r).strip()
            ]
            items.sort(key=lambda x: -x["epoch"])
            return items[:limit]
        except Exception:
            logger.warning("list_facts(%s) failed", namespace, exc_info=True)
            return []

    if not MEMORY_ID:
        return {"explicit": [], "auto": []}
    return {
        "explicit": _list(EXPLICIT_NS_TMPL.format(actorId=actor_id), explicit_limit),
        "auto": _list(FACTS_NS_TMPL.format(actorId=actor_id), auto_limit),
    }


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
    """Directly persist an explicit fact. Used by the `remember` tool.

    Writes to the EXPLICIT namespace with NO strategy association: still
    semantically searchable (indexing takes ~20-60s; the immediacy gap is
    covered by recent_events() re-seeding), but outside the SEMANTIC strategy's
    consolidation domain — the service's background consolidation cannot
    silently rewrite or retire a fact the user dictated. Returns True on success.
    """
    text = (text or "").strip()
    if not MEMORY_ID or not text:
        return False
    explicit_ns = EXPLICIT_NS_TMPL.format(actorId=actor_id)
    record = {
        "requestIdentifier": f"{actor_id}-{uuid.uuid4().hex}",
        "namespaces": [explicit_ns],
        "content": {"text": text},
        "timestamp": int(time.time()),
    }
    try:
        _c().batch_create_memory_records(memoryId=MEMORY_ID, records=[record])
        return True
    except Exception:
        logger.error("remember_fact failed", exc_info=True)
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
        # ERROR, not WARNING: a failed record_turn means the turn silently never
        # reaches long-term memory (this exact failure mode lost turns during a
        # memory-resource migration). Alarm on this in CloudWatch.
        logger.error("memory.record_turn failed (turn not persisted)", exc_info=True)


# ---------------------------------------------------------------------------
# Curate (forget / supersede)
# ---------------------------------------------------------------------------

# Candidates scoring below this are noise, not matches — semantic search always
# returns SOMETHING (nearest neighbor), which is exactly how a code-name query
# once deleted two unrelated KMS memories. Deletion additionally requires an
# exact record id (see delete_record), so this floor only trims the候选 list.
MIN_FORGET_SCORE = 0.35


def find_facts(actor_id: str, query: str, top_k: int = 3) -> list[dict]:
    """Phase 1 of forgetting: return candidate facts matching `query`, WITHOUT
    deleting anything. Each candidate: {"id", "text", "score", "layer"} where
    layer is "explicit" (user-dictated) or "auto" (extracted/ambient). Explicit
    candidates come first — what the user wants forgotten is almost always
    something they dictated."""
    if not MEMORY_ID or not query:
        return []
    out: list[dict] = []
    layers = [
        ("explicit", EXPLICIT_NS_TMPL.format(actorId=actor_id)),
        ("auto", FACTS_NS_TMPL.format(actorId=actor_id)),
    ]
    for layer, ns in layers:
        try:
            resp = _c().retrieve_memory_records(
                memoryId=MEMORY_ID,
                namespace=ns,
                searchCriteria={"searchQuery": query, "topK": top_k},
            )
            for rec in resp.get("memoryRecordSummaries", []) or []:
                rid, text = _rec_id(rec), _rec_text(rec)
                score = float(rec.get("score") or 0)
                if rid and text and score >= MIN_FORGET_SCORE:
                    out.append({"id": rid, "text": text, "score": score, "layer": layer})
        except Exception:
            logger.warning("find_facts(%s) failed", layer, exc_info=True)
    return out[: top_k * 2]


def delete_record(record_id: str) -> bool:
    """Phase 2 of forgetting: delete ONE record by its exact id (which must come
    from a find_facts() candidate the agent/user just confirmed)."""
    if not MEMORY_ID or not record_id:
        return False
    try:
        _c().delete_memory_record(memoryId=MEMORY_ID, memoryRecordId=record_id)
        return True
    except Exception:
        logger.error("delete_record failed", exc_info=True)
        return False
