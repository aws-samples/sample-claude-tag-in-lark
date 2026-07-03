"""Weekly memory gardener — hygiene pass over the AUTO memory layer.

The auto layer (/actor/{chat}/facts) accumulates three kinds of weeds that the
service's own consolidation does not reliably clean up:

  1. Duplicates — the explicit `remember` tool and async extraction routinely
     double-write the same fact; ambient sweeps add near-copies.
  2. Memory-ops echo — conversations ABOUT memory ("delete that", "I don't
     recognize this") get re-extracted into meta-records about the operation.
  3. Troubleshooting residue — one-off debugging threads leave intermediate
     state records long after the issue is resolved; only the conclusion is
     worth keeping.

Once a week, for each enrolled chat, a conservative Haiku pass reviews the auto
layer and proposes actions; this Lambda applies them under hard guardrails:

  - The EXPLICIT layer (/actor/{chat}/explicit) is NEVER modified. It is passed
    to the model read-only so duplicated auto copies of protected facts can be
    identified — the auto copy is the deletable one.
  - Records younger than MIN_AGE_DAYS are untouchable (extraction may still be
    settling; recent context is still "current", not residue).
  - At most MAX_DELETES_PER_CHAT deletions per chat per run.
  - When unsure, keep — the prompt is explicit that deletion needs certainty.
  - Every action is logged with the model's reason; event {"dry_run": true}
    computes and returns actions without applying anything.

Self-contained: Bedrock (Haiku, direct) + DynamoDB (chat enumeration via the
consolidation cursor table) + AgentCore Memory. Not in a VPC (public AWS APIs),
same posture as the consolidator.
"""

import json
import logging
import os
import time
import uuid

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-west-2")
CURSOR_TABLE = os.environ.get("CONSOLIDATION_TABLE", "lark-claude-tag-consolidation")
MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
SEMANTIC_STRATEGY_ID = os.environ.get("MEMORY_SEMANTIC_STRATEGY_ID", "")
FACTS_NS_TMPL = os.environ.get("MEMORY_NAMESPACE_TMPL", "/actor/{actorId}/facts")
EXPLICIT_NS_TMPL = os.environ.get("MEMORY_EXPLICIT_NS_TMPL", "/actor/{actorId}/explicit")
HAIKU_MODEL = os.environ.get("GARDEN_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
MIN_AGE_DAYS = int(os.environ.get("MIN_AGE_DAYS", "7") or 7)
MAX_DELETES_PER_CHAT = int(os.environ.get("MAX_DELETES_PER_CHAT", "10") or 10)

_GARDEN_SYSTEM = """你是一个谨慎的长期记忆园丁。下面给你一个群的两组记忆:
- 【受保护记忆】:用户明确要求记住的,绝对不许动,只作参照。
- 【自动记忆】:系统从对话里自动蒸馏的,带编号,是你唯一可以整理的对象。

你的任务是找出自动记忆里明确该清理的条目,只有三类:
1. 重复:与另一条自动记忆、或与某条受保护记忆说的是同一件事(表述略异也算)。删掉多余的,同一事实保留信息最全的那条(与受保护记忆重复时,删自动那条)。
2. 记忆操作回声:内容是关于"记忆本身的操作"的元记录(如「用户已要求删除某条记忆」「用户对某条记录没有印象/不认可」),操作早已完成,记录本身无长期价值。
3. 已了结的排障残留:一次性技术排查的中间过程(报错现象、逐步假设),且已有明确结论。中间过程可删,结论必须保留。

铁律:
- 拿不准 = 保留。宁可留十条废话,不删一条有用的。
- 人物角色、项目约定、固定流程、用户偏好——除非重复,一律保留。
- 结论性事实(问题根因、最终方案)一律保留。

输出严格 JSON:{"delete": [{"idx": 编号, "why": "一句话理由"}, ...]}。没有要删的就输出 {"delete": []}。只输出 JSON。"""

_bedrock = None
_agentcore = None
_ddb = None


def _bedrock_client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock


def _ac():
    global _agentcore
    if _agentcore is None:
        _agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
    return _agentcore


def _enrolled_chats() -> list[str]:
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION).Table(CURSOR_TABLE)
    ids: list[str] = []
    try:
        resp = _ddb.scan(ProjectionExpression="chat_id")
        ids += [i["chat_id"] for i in resp.get("Items", []) if i.get("chat_id")]
        while "LastEvaluatedKey" in resp:
            resp = _ddb.scan(ProjectionExpression="chat_id", ExclusiveStartKey=resp["LastEvaluatedKey"])
            ids += [i["chat_id"] for i in resp.get("Items", []) if i.get("chat_id")]
    except Exception:
        logger.exception("scan of cursor table failed")
    return ids


def _list_ns(namespace: str, limit: int = 100) -> list[dict]:
    try:
        resp = _ac().list_memory_records(memoryId=MEMORY_ID, namespace=namespace, maxResults=limit)
        recs = resp.get("memoryRecordSummaries") or []
        out = []
        for r in recs:
            text = ((r.get("content") or {}).get("text") or "").strip()
            rid = r.get("memoryRecordId") or ""
            ts = r.get("createdAt")
            try:
                epoch = ts.timestamp()
            except AttributeError:
                epoch = 0.0
            if text and rid:
                out.append({"id": rid, "text": text, "epoch": epoch})
        return out
    except Exception:
        logger.exception("list %s failed", namespace)
        return []


def _propose(protected: list[str], eligible: list[dict]) -> list[dict]:
    """Ask Haiku which eligible auto records to delete. Returns
    [{"idx", "why"}], [] on any doubt/parse failure (fail-safe = keep)."""
    lines = ["【受保护记忆】(只读参照):"]
    lines += [f"- {t}" for t in protected] if protected else ["(无)"]
    lines.append("")
    lines.append("【自动记忆】(可整理,带编号):")
    for i, r in enumerate(eligible, 1):
        day = time.strftime("%Y-%m-%d", time.gmtime(r["epoch"])) if r["epoch"] else "?"
        lines.append(f"{i}. [{day}] {r['text']}")
    try:
        resp = _bedrock_client().converse(
            modelId=HAIKU_MODEL,
            system=[{"text": _GARDEN_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": "\n".join(lines)}]}],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
        out = resp["output"]["message"]["content"][0]["text"].strip()
    except Exception:
        logger.exception("gardener model call failed")
        return []
    if out.startswith("```"):
        out = out.strip("`")
        out = out[4:] if out[:4].lower() == "json" else out
    try:
        actions = json.loads(out).get("delete", [])
    except (json.JSONDecodeError, AttributeError):
        logger.warning("gardener returned non-JSON: %s", out[:200])
        return []
    clean = []
    for a in actions:
        try:
            idx = int(a.get("idx"))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(eligible):
            clean.append({"idx": idx, "why": str(a.get("why", ""))[:120]})
    return clean[:MAX_DELETES_PER_CHAT]


def _garden_chat(chat_id: str, dry_run: bool, min_age_days: int) -> dict:
    facts_ns = FACTS_NS_TMPL.format(actorId=chat_id)
    auto = _list_ns(facts_ns)
    protected = [r["text"] for r in _list_ns(EXPLICIT_NS_TMPL.format(actorId=chat_id))]
    cutoff = time.time() - min_age_days * 86400
    eligible = [r for r in auto if r["epoch"] and r["epoch"] < cutoff]
    if len(eligible) < 2:
        return {"chat": chat_id, "auto": len(auto), "eligible": len(eligible), "deleted": 0, "actions": []}
    proposals = _propose(protected, eligible)
    applied = []
    for p in proposals:
        rec = eligible[p["idx"] - 1]
        entry = {"text": rec["text"][:80], "why": p["why"]}
        if not dry_run:
            try:
                _ac().delete_memory_record(memoryId=MEMORY_ID, memoryRecordId=rec["id"])
            except Exception:
                logger.exception("delete failed for %s", rec["id"])
                continue
        applied.append(entry)
        logger.info("garden chat=%s %s: %s | why: %s",
                    chat_id, "WOULD-DELETE" if dry_run else "DELETED", rec["text"][:80], p["why"])
    return {"chat": chat_id, "auto": len(auto), "eligible": len(eligible),
            "deleted": len(applied), "actions": applied}


def lambda_handler(event, context):
    if not MEMORY_ID:
        logger.error("AGENTCORE_MEMORY_ID not set")
        return {"error": "no memory id"}
    event = event or {}
    dry_run = bool(event.get("dry_run"))
    # Manual invocations may narrow the sweep ({"actors": [...]}) or lower the
    # age floor ({"min_age_days": 0}) — e.g. an on-demand gardening pass right
    # after a messy conversation, or a dry-run rehearsal on fresh records.
    try:
        min_age_days = int(event.get("min_age_days", MIN_AGE_DAYS))
    except (TypeError, ValueError):
        min_age_days = MIN_AGE_DAYS
    chats = event.get("actors") or _enrolled_chats()
    results = [_garden_chat(c, dry_run, min_age_days) for c in chats]
    total = sum(r["deleted"] for r in results)
    logger.info("gardening done: chats=%d deleted=%d dry_run=%s", len(chats), total, dry_run)
    return {"dry_run": dry_run, "chats": len(chats), "deleted": total, "results": results}
