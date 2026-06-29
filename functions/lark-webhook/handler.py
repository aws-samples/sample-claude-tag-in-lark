"""Lambda entry point for Lark webhook events.

Thin adapter with two modes:
1. Webhook mode (from API Gateway): decrypt, verify, dedup, ack, async self-invoke.
2. Async mode (self-invoked): run the agent flow and post the reply.

The actual agent work happens in AgentCore Runtime (see event_handlers.py).
"""

import base64
import json
import logging
import os
import uuid
from collections import OrderedDict

import boto3

from lark_auth import decrypt_event, verify_signature
from lark_secrets import get_secret
from event_handlers import handle_message_async, handle_scheduled_task, invoke_agent_streaming

logger = logging.getLogger()
logger.setLevel(logging.INFO)

FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")

# Track processed event IDs to handle Lark retries (LRU eviction)
_processed_events: "OrderedDict[str, bool]" = OrderedDict()
MAX_PROCESSED_EVENTS = 500

_lambda_client = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def lambda_handler(event, context):
    # --- Smoke test: verify AgentCore connectivity, no Lark calls ---
    if event.get("_smoke_test"):
        return _handle_smoke_test()

    # --- Async processing mode (self-invoked) ---
    if event.get("_async_process"):
        logger.info("Async processing mode")
        try:
            handle_message_async(event["event_data"])
        except Exception as e:
            logger.error("Async processing failed: %s", e, exc_info=True)
        return

    # --- Scheduled-task delivery mode (invoked by the dispatcher Lambda) ---
    if event.get("_scheduled_task"):
        logger.info("Scheduled-task mode: chat=%s mode=%s", event.get("chat_id"), event.get("mode"))
        try:
            handle_scheduled_task(
                event.get("chat_id", ""),
                event.get("mode", "remind"),
                event.get("payload", ""),
                event.get("mention_open_id", ""),
                event.get("title", ""),
            )
        except Exception as e:
            logger.error("Scheduled-task processing failed: %s", e, exc_info=True)
        return

    # --- Webhook mode (from API Gateway) ---
    body_str = event.get("body", "")
    if event.get("isBase64Encoded"):
        body_str = base64.b64decode(body_str).decode("utf-8")

    try:
        body = json.loads(body_str)
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    # Decrypt if encrypted
    if "encrypt" in body:
        try:
            body = json.loads(decrypt_event(body["encrypt"]))
        except Exception as e:
            logger.error("Decryption failed: %s", e)
            return _response(400, {"error": "Decryption failed"})

    # URL verification challenge
    if "challenge" in body:
        if body.get("token", "") != get_secret("LARK_VERIFICATION_TOKEN"):
            logger.warning("Challenge token mismatch")
            return _response(403, {"error": "Token mismatch"})
        return _response(200, {"challenge": body["challenge"]})

    # Verify signature (mandatory for event callbacks)
    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    timestamp = headers.get("x-lark-request-timestamp", "")
    nonce = headers.get("x-lark-request-nonce", "")
    signature = headers.get("x-lark-signature", "")
    if not signature:
        return _response(403, {"error": "Missing signature"})
    if not verify_signature(timestamp, nonce, body_str, signature):
        return _response(403, {"error": "Invalid signature"})

    # Verify token in header
    header = body.get("header", {})
    if header.get("token", "") != get_secret("LARK_VERIFICATION_TOKEN"):
        return _response(403, {"error": "Token mismatch"})

    # Deduplicate (Lark retries if no 200 within 3s)
    event_id = header.get("event_id", "")
    if event_id:
        if event_id in _processed_events:
            logger.info("Duplicate event %s, skipping", event_id)
            return _response(200, {"msg": "ok"})
        _processed_events[event_id] = True
        while len(_processed_events) > MAX_PROCESSED_EVENTS:
            _processed_events.popitem(last=False)

    event_type = header.get("event_type", "")
    event_data = body.get("event", {})

    if event_type == "im.message.receive_v1":
        try:
            _get_lambda_client().invoke(
                FunctionName=FUNCTION_NAME,
                InvocationType="Event",
                Payload=json.dumps({"_async_process": True, "event_data": event_data}),
            )
            logger.info("Async invoke dispatched for event %s", event_id)
        except Exception as e:
            logger.error("Error dispatching async: %s", e, exc_info=True)
    else:
        logger.info("Unhandled event type: %s", event_type)

    return _response(200, {"msg": "ok"})


def _handle_smoke_test() -> dict:
    """Verify AgentCore connectivity (no Lark API calls)."""
    session_id = f"smoke-{uuid.uuid4().hex}"
    try:
        reply, tools = "", []
        for accumulated_text, tool_name in invoke_agent_streaming(session_id, "Say hello in one word."):
            reply = accumulated_text
            if tool_name:
                tools.append(tool_name)
        logger.info("Smoke OK: reply_len=%d tools=%s", len(reply), tools)
        return _response(200, {"ok": True, "reply_len": len(reply), "tools": tools})
    except Exception as e:
        logger.error("Smoke FAIL: %s", e, exc_info=True)
        return _response(500, {"ok": False, "error": str(e)})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
