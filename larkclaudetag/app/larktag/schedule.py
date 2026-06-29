"""Scheduled tasks / reminders — DynamoDB-backed job registry (agent side).

The agent's schedule_task / list_tasks / cancel_task tools call into here. A
separate dispatcher Lambda (functions/dispatcher/) scans the same table every
minute and fires due jobs, so this module only WRITES jobs and lists/cancels
them — it never delivers.

Time is stamped from the container clock here (authoritative), so the model only
ever passes relative quantities (delay_seconds / every_seconds / count) plus an
optional absolute `until_epoch`; it never has to guess the wall clock.

Everything is wrapped defensively: a missing table / API mismatch degrades to a
returned error string rather than crashing the agent turn.

Schema (mirrors template.yaml SchedulesTable):
  job_id(PK) status next_run_epoch interval_seconds remaining_count until_epoch
  chat_id creator_open_id mode payload title created_at last_fired_at ttl
"""

import logging
import os
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Attr, Key

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("SCHEDULE_TABLE_NAME", "")
REGION = os.environ.get("AWS_REGION", "us-west-2")

# --- Guardrails (also enforced here, not just in the prompt) ---
MIN_INTERVAL_S = 60            # no sub-minute reminders (granularity + anti-spam)
MAX_COUNT = 100                # max occurrences for a counted job
MAX_HORIZON_S = 30 * 24 * 3600  # recurring jobs auto-expire after 30 days
MAX_ACTIVE_PER_CHAT = 20       # cap concurrent jobs per chat
_DONE_TTL_S = 7 * 24 * 3600    # safety auto-purge buffer

_table = None


def _t():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    return _table


def enabled() -> bool:
    return bool(TABLE_NAME)


def _active_count(chat_id: str) -> int:
    try:
        resp = _t().query(
            IndexName="chat-index",
            KeyConditionExpression=Key("chat_id").eq(chat_id),
            FilterExpression=Attr("status").eq("active"),
            Select="COUNT",
        )
        return int(resp.get("Count", 0))
    except Exception:
        logger.warning("active_count query failed", exc_info=True)
        return 0


def create_job(
    chat_id: str,
    creator_open_id: str,
    description: str,
    delay_seconds: int,
    every_seconds: int = 0,
    count: int = -1,
    until_seconds: int = 0,
    mode: str = "remind",
    payload: str = "",
) -> tuple[bool, str]:
    """Create a scheduled job. Returns (ok, message_or_job_id).

    All times are RELATIVE to now (stamped here from the container clock), so the
    model never has to guess the wall clock:
      delay_seconds: seconds from now to the FIRST run.
      every_seconds: repeat interval; 0 = one-shot.
      count: max occurrences; -1 = unlimited (only meaningful when every_seconds>0).
      until_seconds: seconds from now to the end; 0 = none.
      mode: 'remind' (post payload text) or 'agent' (run a turn with payload prompt).
    """
    if not TABLE_NAME:
        return False, "定时功能没配置好(缺表名),先别用。"
    if not chat_id:
        return False, "当前没有群上下文,无法建任务。"
    payload = (payload or description or "").strip()
    if not payload:
        return False, "没有提醒内容 / 任务内容。"
    if mode not in ("remind", "agent"):
        mode = "remind"

    now = int(time.time())
    delay_seconds = max(0, int(delay_seconds or 0))
    every_seconds = int(every_seconds or 0)
    count = int(count if count is not None else -1)
    until_seconds = int(until_seconds or 0)

    # --- Guardrails ---
    if every_seconds and every_seconds < MIN_INTERVAL_S:
        return False, f"重复间隔太短了,最少 {MIN_INTERVAL_S} 秒(1 分钟)。"
    if count != -1 and (count < 1 or count > MAX_COUNT):
        return False, f"次数要在 1~{MAX_COUNT} 之间。"
    if until_seconds and until_seconds > MAX_HORIZON_S:
        return False, "截止时间太远了,最多定到 30 天后。"
    until_epoch = now + until_seconds if until_seconds > 0 else 0
    if _active_count(chat_id) >= MAX_ACTIVE_PER_CHAT:
        return False, f"这个群的活动提醒已经有 {MAX_ACTIVE_PER_CHAT} 个了,先取消几个再加。"

    # One-shot vs recurring bookkeeping.
    if every_seconds <= 0:
        every_seconds = 0
        remaining = 1
    else:
        remaining = count if count != -1 else -1
        # Recurring with no stop condition → auto-cap horizon so it can't run forever.
        if remaining == -1 and until_epoch == 0:
            until_epoch = now + MAX_HORIZON_S

    next_run = now + delay_seconds
    title = (description or payload)[:40]
    ttl = (until_epoch or (next_run + MAX_HORIZON_S)) + _DONE_TTL_S
    job_id = uuid.uuid4().hex

    item = {
        "job_id": job_id,
        "status": "active",
        "next_run_epoch": next_run,
        "interval_seconds": every_seconds,
        "remaining_count": remaining,
        "until_epoch": until_epoch,
        "chat_id": chat_id,
        "creator_open_id": creator_open_id or "",
        "mode": mode,
        "payload": payload,
        "title": title,
        "created_at": now,
        "last_fired_at": 0,
        "ttl": ttl,
    }
    try:
        _t().put_item(Item=item)
    except Exception:
        logger.warning("create_job put_item failed", exc_info=True)
        return False, "任务写入失败,稍后再试。"
    return True, job_id


def list_jobs(chat_id: str) -> list[dict]:
    """Active jobs for this chat, newest first. Best-effort ([] on failure)."""
    if not TABLE_NAME or not chat_id:
        return []
    try:
        resp = _t().query(
            IndexName="chat-index",
            KeyConditionExpression=Key("chat_id").eq(chat_id),
            FilterExpression=Attr("status").eq("active"),
            ScanIndexForward=False,
        )
        return resp.get("Items", []) or []
    except Exception:
        logger.warning("list_jobs query failed", exc_info=True)
        return []


def cancel_job(chat_id: str, target: str) -> tuple[int, str]:
    """Cancel one job (target == job_id) or all active jobs (target == 'all').

    Returns (count_cancelled, message). Only cancels jobs in THIS chat (so a job_id
    from another chat can't be touched).
    """
    if not TABLE_NAME or not chat_id:
        return 0, "当前没有群上下文。"
    target = (target or "").strip()
    if not target:
        return 0, "没说取消哪个(给我 job_id,或者说『全部』)。"

    if target.lower() in ("all", "全部", "所有"):
        jobs = list_jobs(chat_id)
        n = 0
        for j in jobs:
            if _set_cancelled(j["job_id"]):
                n += 1
        return n, (f"已取消 {n} 个提醒。" if n else "本群没有进行中的提醒。")

    # Single job: match by id or id-prefix among THIS chat's active jobs (the list
    # shows an 8-char prefix), which also scopes cancellation to the current chat.
    jobs = list_jobs(chat_id)
    matches = [j for j in jobs if j["job_id"] == target or j["job_id"].startswith(target)]
    if not matches:
        return 0, "没找到这个提醒(可能已结束,或不在本群)。给我 list 里的那个短编号试试。"
    if len(matches) > 1:
        return 0, "这个编号对上了好几个,多给几位编号区分一下。"
    return (1, "已取消。") if _set_cancelled(matches[0]["job_id"]) else (0, "取消失败,稍后再试。")


def _set_cancelled(job_id: str) -> bool:
    try:
        _t().update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #st = :c",
            ConditionExpression="#st = :a",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":c": "cancelled", ":a": "active"},
        )
        return True
    except Exception:
        # ConditionalCheckFailed (already not-active) or transient error — both non-fatal.
        logger.info("set_cancelled noop/failed for %s", job_id, exc_info=True)
        return False


def describe_jobs(chat_id: str) -> str:
    """Human-readable list of this chat's active jobs (for the list_tasks tool)."""
    jobs = list_jobs(chat_id)
    if not jobs:
        return "本群目前没有进行中的提醒/定时任务。"
    now = int(time.time())
    lines = []
    for j in jobs:
        nxt = int(j.get("next_run_epoch", 0)) - now
        when = f"约 {max(0, nxt) // 60} 分钟后" if nxt < 3600 else f"约 {max(0, nxt) // 3600} 小时后"
        every = int(j.get("interval_seconds", 0) or 0)
        rep = "一次性" if every == 0 else f"每 {every // 60} 分钟"
        rem = int(j.get("remaining_count", -1))
        rep += "" if (every == 0 or rem == -1) else f"(还剩 {rem} 次)"
        lines.append(f"- [{j.get('mode','remind')}] {j.get('title','')} — 下次{when},{rep}  `{j['job_id'][:8]}`")
    return "进行中的提醒/任务:\n" + "\n".join(lines)
