"""Generic secret loader for AWS Secrets Manager with env-var fallback.

Returns a ``get_secret(key)`` callable. The Secrets Manager value is fetched
once on first call and cached for the process lifetime.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)


def create_secret_loader(env_var_name: str):
    """Return a ``get_secret(key)`` function backed by the Secrets Manager ARN
    stored in *env_var_name*, with env-var fallback."""
    cache: dict | None = None  # None = not yet loaded

    def get_secret(key: str) -> str:
        nonlocal cache
        arn = os.environ.get(env_var_name, "")
        if not arn:
            return os.environ.get(key, "")
        if cache is None:
            try:
                client = boto3.client("secretsmanager")
                resp = client.get_secret_value(SecretId=arn)
                cache = json.loads(resp["SecretString"])
                logger.info("Loaded secrets from Secrets Manager")
            except Exception:
                logger.exception(
                    "Failed to load from Secrets Manager, falling back to env vars"
                )
                cache = {}  # mark as attempted
                return os.environ.get(key, "")
        return cache.get(key, os.environ.get(key, ""))

    return get_secret
