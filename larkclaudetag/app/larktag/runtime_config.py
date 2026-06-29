"""Load runtime config (incl. secrets) from AWS Secrets Manager into os.environ.

Keeps secrets out of the image and out of the AgentCore runtime env config: the
only thing baked into the image is the non-secret pointer RUNTIME_SECRET_ID. The
runtime exec role needs secretsmanager:GetSecretValue on that secret.

Imported FIRST in main.py (before `import memory`, which reads AGENTCORE_MEMORY_ID
at module load). Uses setdefault so any explicitly-set env var wins.

Secret JSON is expected to hold:
  ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY, LITELLM_MODEL, AGENTCORE_MEMORY_ID,
  MEMORY_SEMANTIC_STRATEGY_ID, SKILL_BUCKET, SCHEDULE_TABLE_NAME, EXA_API_KEY,
  AWS_KNOWLEDGE_MCP_URL, LARK_APP_ID, LARK_APP_SECRET, LARK_OPEN_BASE
"""

import json
import logging
import os

logger = logging.getLogger(__name__)


def _load() -> None:
    secret_id = os.environ.get("RUNTIME_SECRET_ID", "")
    if not secret_id:
        logger.info("RUNTIME_SECRET_ID not set; relying on existing env vars")
        return
    try:
        import boto3

        client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-west-2"))
        data = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
        for k, v in data.items():
            os.environ.setdefault(k, str(v))
        logger.info("Loaded %d runtime config keys from %s", len(data), secret_id)
    except Exception:
        # %s is secret_id (the Secrets Manager name/ARN — an identifier, not the
        # value); the SecretString parsed above is never written to this log.
        logger.exception("Failed to load runtime config (id=%s); continuing with env", secret_id)


_load()

# After secrets land in os.environ, pull the agent's learned skills from S3 into
# the local skills dir so the `claude` CLI discovers them at launch. Deferred import
# so skill_store's module-level config reads the now-populated env (SKILL_BUCKET).
try:
    import skill_store

    skill_store.sync_down()
except Exception:  # noqa: BLE001 — never let skill sync block startup
    logger.exception("startup skill sync failed (continuing with vendored skills)")
