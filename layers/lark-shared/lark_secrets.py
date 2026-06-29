"""Lark bot secret loader — delegates to platform_secrets.

Secret keys expected in the bundle (Secrets Manager JSON or env vars):
  LARK_APP_ID, LARK_APP_SECRET, LARK_ENCRYPT_KEY, LARK_VERIFICATION_TOKEN
"""

from platform_secrets import create_secret_loader

get_secret = create_secret_loader("LARK_SECRETS_ARN")
