"""Lark webhook auth: signature verification, event decryption, tenant token.

The Lark OpenAPI base is configurable via the LARK_OPEN_BASE env var
(defaults to the international endpoint).
"""

import base64
import hashlib
import hmac
import logging
import os
import time

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from lark_secrets import get_secret

logger = logging.getLogger(__name__)

# International Lark by default; set LARK_OPEN_BASE=https://open.feishu.cn for cn.
LARK_OPEN_BASE = os.environ.get("LARK_OPEN_BASE", "https://open.larksuite.com")

# Maximum allowed age (seconds) for a request timestamp before it's rejected
TIMESTAMP_MAX_AGE_S = 300  # 5 minutes

# Module-level token cache
_token_cache = {"token": None, "expire_at": 0}


def invalidate_token_cache():
    """Force-clear the cached token (call on 401 from Lark API)."""
    _token_cache["token"] = None
    _token_cache["expire_at"] = 0


def decrypt_event(encrypt_data: str) -> str:
    """Decrypt Lark encrypted event payload (AES-256-CBC, key=SHA256(encrypt_key), IV=first 16B)."""
    key = hashlib.sha256(get_secret("LARK_ENCRYPT_KEY").encode("utf-8")).digest()
    ciphertext = base64.b64decode(encrypt_data)
    iv = ciphertext[:16]
    encrypted = ciphertext[16:]

    # AES-256-CBC is mandated by Lark/Feishu's event-encryption spec: the server
    # encrypts with CBC and we must decrypt with the same mode, so we cannot switch
    # to an AEAD mode. Integrity is enforced separately by verify_signature()
    # (HMAC-SHA256 over timestamp+nonce+encrypt_key+body) plus the HTTPS transport.
    # nosemgrep: crypto-mode-without-authentication
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(encrypted) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


def verify_signature(timestamp: str, nonce: str, body: str, signature: str) -> bool:
    """Verify X-Lark-Signature = SHA256(timestamp + nonce + encrypt_key + body).

    Also rejects stale timestamps (> TIMESTAMP_MAX_AGE_S) to mitigate replay.
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        logger.warning("Invalid timestamp format: %r", timestamp)
        return False
    if abs(time.time() - ts) > TIMESTAMP_MAX_AGE_S:
        logger.warning("Stale timestamp rejected: %s", timestamp)
        return False

    raw = f"{timestamp}{nonce}{get_secret('LARK_ENCRYPT_KEY')}{body}"
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def get_tenant_access_token() -> str:
    """Get tenant_access_token with module-level caching (2h validity, refresh 5min early)."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire_at"]:
        return _token_cache["token"]

    resp = requests.post(
        f"{LARK_OPEN_BASE}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": get_secret("LARK_APP_ID"), "app_secret": get_secret("LARK_APP_SECRET")},
        timeout=5,
        verify=True,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code") != 0:
        raise RuntimeError("Failed to get tenant_access_token: code=%s" % data.get("code"))

    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = now + data.get("expire", 7200) - 300
    return _token_cache["token"]
