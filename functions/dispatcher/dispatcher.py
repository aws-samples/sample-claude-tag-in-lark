"""Heartbeat dispatcher for scheduled tasks / reminders.

Fires every minute (EventBridge rate(1 minute), wired in template.yaml). Each run:
  1. Query the `due-index` GSI for active jobs whose next_run_epoch <= now.
  2. For each, CLAIM-then-advance with a single conditional UpdateItem
     (ConditionExpression pins next_run_epoch + status=active) so an overlapping
     tick or concurrent invocation can't double-fire the same occurrence.
  3. On a successful claim, async-invoke the webhook Lambda to actually deliver
     (remind = post text; agent = run a turn). Claim-then-deliver is at-most-once:
     if the invoke fails the occurrence is dropped (logged) rather than risking a
     spammy double-send.

The webhook Lambda owns all Lark posting; this function never touches Lark.
"""

import json
import logging
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("SCHEDULES_TABLE", "lark-claude-tag-schedules")
WEBHOOK_FUNCTION_NAME = os.environ.get("WEBHOOK_FUNCTION_NAME", "")
MAX_PER_TICK = 100  # safety cap on jobs processed in one minute
DONE_TTL_S = 7 * 24 * 3600  # auto-purge finished rows 7 days later

_ddb = None
_lambda = None


def _table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _ddb


def _lambda_client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


def _due_jobs(now: int) -> list[dict]:
    """Active jobs whose next_run_epoch <= now (via due-index), oldest-due first."""
    resp = _table().query(
        IndexName="due-index",
        KeyConditionExpression=Key("status").eq("active") & Key("next_run_epoch").lte(now),
        Limit=MAX_PER_TICK,
    )
    return resp.get("Items", []) or []


def _claim_and_advance(job: dict, now: int) -> bool:
    """Atomically claim this occurrence and advance/retire the job.

    Returns True if WE won the claim (caller should deliver), False if another
    tick already advanced it (ConditionalCheckFailed) or on error.
    """
    job_id = job["job_id"]
    seen = int(job["next_run_epoch"])
    interval = int(job.get("interval_seconds", 0) or 0)
    remaining = int(job.get("remaining_count", -1))
    until = int(job.get("until_epoch", 0) or 0)

    new_remaining = remaining - 1 if remaining > 0 else remaining
    new_next = now + interval if interval > 0 else 0

    retire = (
        interval <= 0  # one-shot
        or (remaining != -1 and new_remaining <= 0)  # count exhausted
        or (until > 0 and (new_next == 0 or new_next > until))  # past end
    )

    # Condition: nobody else has moved this occurrence (pin next_run_epoch + active).
    cond = "next_run_epoch = :seen AND #st = :active"
    names = {"#st": "status"}
    try:
        if retire:
            _table().update_item(
                Key={"job_id": job_id},
                UpdateExpression="SET #st = :done, last_fired_at = :now, #ttl = :ttl",
                ConditionExpression=cond,
                ExpressionAttributeNames={**names, "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":seen": seen, ":active": "active",
                    ":done": "done", ":now": now, ":ttl": now + DONE_TTL_S,
                },
            )
        else:
            _table().update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET next_run_epoch = :next, remaining_count = :rem, last_fired_at = :now"
                ),
                ConditionExpression=cond,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={
                    ":seen": seen, ":active": "active",
                    ":next": new_next, ":rem": new_remaining, ":now": now,
                },
            )
        return True
    except _table().meta.client.exceptions.ConditionalCheckFailedException:
        logger.info("job %s already advanced by another tick; skipping", job_id)
        return False
    except Exception:
        logger.warning("claim/advance failed for job %s", job_id, exc_info=True)
        return False


def _deliver(job: dict) -> None:
    """Ask the webhook Lambda to deliver this occurrence (async / at-most-once)."""
    if not WEBHOOK_FUNCTION_NAME:
        logger.error("WEBHOOK_FUNCTION_NAME not set; cannot deliver job %s", job.get("job_id"))
        return
    payload = {
        "_scheduled_task": True,
        "chat_id": job.get("chat_id", ""),
        "mode": job.get("mode", "remind"),
        "payload": job.get("payload", ""),
        "mention_open_id": job.get("creator_open_id", ""),
        "title": job.get("title", ""),
    }
    try:
        _lambda_client().invoke(
            FunctionName=WEBHOOK_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except Exception:
        # at-most-once: a failed invoke drops this occurrence rather than retrying
        # (a retry storm would spam the chat). Logged for visibility.
        logger.warning("deliver invoke failed for job %s", job.get("job_id"), exc_info=True)


def lambda_handler(event, context):
    now = int(time.time())
    jobs = _due_jobs(now)
    logger.info("tick now=%d due=%d", now, len(jobs))
    fired = 0
    for job in jobs:
        if _claim_and_advance(job, now):
            _deliver(job)
            fired += 1
    logger.info("tick done: fired=%d of due=%d", fired, len(jobs))
    return {"due": len(jobs), "fired": fired}
