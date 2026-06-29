"""Hourly ambient-memory consolidator (group-awareness tier 2).

Short-term context is served live by the agent's ``read_chat_history`` (no
storage). This Lambda handles the long tail: the non-@ chatter the bot would
otherwise never persist. Once an hour it sweeps each enrolled chat, distills
ONLY durable facts with a deliberately conservative Haiku pass, and writes them
into the SAME per-channel AgentCore Memory the agent already recalls from — so
the next @mention already knows what the group has been discussing.

Per run, for each enrolled chat (the webhook enrolls a chat the first time the
bot is used there):
  1. Pull messages created after the chat's cursor (Lark ``start_time``).
     No new human messages -> do nothing (no model call, no write, no churn).
  2. Conservative extraction: only decisions / who-is-who / commitments / stable
     preferences survive; banter, logistics, one-off Q&A are dropped.
  3. Write each fact to the channel's facts namespace, prefixed "[群聊旁听] " so
     it is identifiable (and removable) and clearly sourced as ambient, not a
     thing the user explicitly asked to remember.
  4. Advance the cursor to the newest message seen.

Self-contained: Lark (tenant token, via the shared layer) + Bedrock (Haiku,
direct) + DynamoDB (cursor) + AgentCore Memory (direct). This Lambda is NOT in a
VPC, so it reaches AgentCore over the public API and needs no VPC-endpoint-policy
change (unlike the in-VPC agent runtime).
"""

import json
import logging
import os
import time
import uuid

import boto3

from lark_api import iter_chat_messages_since  # provided by the lark-shared layer

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-west-2")
CURSOR_TABLE = os.environ.get("CONSOLIDATION_TABLE", "lark-claude-tag-consolidation")
MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
SEMANTIC_STRATEGY_ID = os.environ.get("MEMORY_SEMANTIC_STRATEGY_ID", "")
FACTS_NS_TMPL = os.environ.get("MEMORY_NAMESPACE_TMPL", "/actor/{actorId}/facts")
HAIKU_MODEL = os.environ.get("EXTRACT_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
MAX_MSGS_PER_RUN = int(os.environ.get("MAX_MSGS_PER_RUN", "200") or 200)
MAX_FACTS_PER_RUN = int(os.environ.get("MAX_FACTS_PER_RUN", "15") or 15)
SOURCE_TAG = "[群聊旁听] "

# The conservative extractor. The #1 risk of ambient memory is poisoning the
# recall index with chatter, so the bar is high and the instruction is explicit:
# when in doubt, drop it.
_EXTRACT_SYSTEM = """你是一个严格的群聊信息提炼器。下面是一段群成员的聊天记录(没有 @ 机器人)。\
你的任务:只挑出【以后还用得上的稳定事实】,写进长期记忆。

只保留这几类:
- 明确的决定或结论(谁定了什么、采用什么方案/口径)
- 人和角色(谁是谁、负责什么、客户或团队成员的身份)
- 承诺与安排(明确的截止日期、谁负责某件事 —— 仅当清楚且不是临时闲聊)
- 稳定的偏好或约定(固定流程、命名规则、统一口径)

坚决丢弃:寒暄、表情、情绪、八卦、临时约时间的来回、一次性的问答、能当场算出来的东西、含糊的猜测、还没定下来的讨论。

输出严格的 JSON,形如 {"facts": ["一句话事实", ...]}。要求:
- 每条事实独立、自包含,带上主语(别用"他/这个/那边"指代)。
- 用中文,简短一句。
- 没有任何值得长期记的,就输出 {"facts": []}。
- 宁可少,不可滥;拿不准的一律不收。
只输出 JSON,不要任何解释。"""

_ddb = None
_bedrock = None
_agentcore = None


def _table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION).Table(CURSOR_TABLE)
    return _ddb


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock


def _agentcore_client():
    global _agentcore
    if _agentcore is None:
        _agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
    return _agentcore


# ---------------------------------------------------------------------------
# Cursor table
# ---------------------------------------------------------------------------

def _enrolled_chats() -> list[dict]:
    """All chats the bot has been used in (tiny table; scan is fine)."""
    rows: list[dict] = []
    try:
        resp = _table().scan(ProjectionExpression="chat_id, last_ts_ms")
        rows += resp.get("Items", []) or []
        while "LastEvaluatedKey" in resp:
            resp = _table().scan(
                ProjectionExpression="chat_id, last_ts_ms",
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            rows += resp.get("Items", []) or []
    except Exception:
        logger.exception("scan of cursor table failed")
    return rows


def _advance(chat_id: str, new_ts_ms: int) -> None:
    try:
        _table().update_item(
            Key={"chat_id": chat_id},
            UpdateExpression="SET last_ts_ms = :t",
            # Never move the cursor backwards (defensive against out-of-order runs).
            ConditionExpression="attribute_not_exists(last_ts_ms) OR last_ts_ms < :t",
            ExpressionAttributeValues={":t": int(new_ts_ms)},
        )
    except _table().meta.client.exceptions.ConditionalCheckFailedException:
        pass
    except Exception:
        logger.warning("advance cursor failed for %s", chat_id, exc_info=True)


# ---------------------------------------------------------------------------
# Message filtering / formatting
# ---------------------------------------------------------------------------

def _is_human_text(msg: dict) -> bool:
    """Keep only human-authored text/post messages (skip system + bot's own)."""
    if (msg.get("sender", {}) or {}).get("sender_type") != "user":
        return False
    return msg.get("msg_type") in ("text", "post")


def _msg_text(msg: dict) -> str:
    raw = (msg.get("body", {}) or {}).get("content", "")
    try:
        content = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return ""
    if msg.get("msg_type") == "text":
        return (content.get("text") or "").strip()
    if msg.get("msg_type") == "post":
        parts = []
        for block in content.get("content", []) or []:
            for run in block or []:
                if isinstance(run, dict) and run.get("tag") == "text":
                    parts.append(run.get("text", ""))
        return " ".join(parts).strip()
    return ""


def _format(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        text = _msg_text(m)
        if not text:
            continue
        sender = ((m.get("sender", {}) or {}).get("id", "") or "")[:10] or "?"
        lines.append(f"[{sender}] {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Extraction + write
# ---------------------------------------------------------------------------

def _extract(transcript: str) -> list[str]:
    """Conservative Haiku extraction -> list of durable fact strings (may be [])."""
    if not transcript.strip():
        return []
    try:
        resp = _bedrock_client().converse(
            modelId=HAIKU_MODEL,
            system=[{"text": _EXTRACT_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": transcript}]}],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
        out = resp["output"]["message"]["content"][0]["text"].strip()
    except Exception:
        logger.exception("Haiku extraction failed")
        return []
    # Tolerate a ```json fence around the object.
    if out.startswith("```"):
        out = out.strip("`")
        out = out[4:] if out[:4].lower() == "json" else out
    try:
        facts = json.loads(out).get("facts", [])
    except (json.JSONDecodeError, AttributeError):
        logger.warning("extractor returned non-JSON: %s", out[:200])
        return []
    clean = [str(f).strip() for f in facts if str(f).strip()]
    return clean[:MAX_FACTS_PER_RUN]


def _write_facts(chat_id: str, facts: list[str]) -> None:
    if not MEMORY_ID or not facts:
        return
    ns = FACTS_NS_TMPL.format(actorId=chat_id)
    now = int(time.time())
    records = []
    for f in facts:
        rec = {
            "requestIdentifier": f"{chat_id}-{uuid.uuid4().hex}",
            "namespaces": [ns],
            "content": {"text": SOURCE_TAG + f},
            "timestamp": now,
        }
        if SEMANTIC_STRATEGY_ID:
            rec["memoryStrategyId"] = SEMANTIC_STRATEGY_ID
        records.append(rec)
    for i in range(0, len(records), 100):  # BatchCreate caps at 100/req
        _agentcore_client().batch_create_memory_records(memoryId=MEMORY_ID, records=records[i:i + 100])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    if not MEMORY_ID:
        logger.error("AGENTCORE_MEMORY_ID not set; nothing to consolidate into")
        return {"error": "no memory id"}

    chats = _enrolled_chats()
    total_facts = 0
    swept = 0
    for row in chats:
        chat_id = row.get("chat_id", "")
        if not chat_id:
            continue
        cursor_ms = int(row.get("last_ts_ms", 0) or 0)
        try:
            msgs = iter_chat_messages_since(chat_id, cursor_ms, max_msgs=MAX_MSGS_PER_RUN)
        except Exception:
            logger.exception("pull failed for chat=%s", chat_id)
            continue
        if not msgs:
            continue  # no new conversation -> do nothing
        swept += 1
        newest_ms = max(int(m.get("create_time", "0") or 0) for m in msgs)
        human = [m for m in msgs if _is_human_text(m)]
        facts = _extract(_format(human)) if human else []
        if facts:
            try:
                _write_facts(chat_id, facts)
                total_facts += len(facts)
            except Exception:
                logger.exception("write_facts failed for chat=%s", chat_id)
        # Advance regardless so system/bot messages aren't re-pulled forever.
        _advance(chat_id, newest_ms)
        logger.info(
            "chat=%s pulled=%d human=%d facts=%d cursor->%d",
            chat_id, len(msgs), len(human), len(facts), newest_ms,
        )
    logger.info("consolidation done: chats=%d swept=%d facts=%d", len(chats), swept, total_facts)
    return {"chats": len(chats), "swept": swept, "facts": total_facts}
