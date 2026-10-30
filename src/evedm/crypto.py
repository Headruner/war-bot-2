"""Token encryption at rest (TOK-01) and log redaction helpers."""

from __future__ import annotations

import base64
import hashlib
import re

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    """Fernet (AES-128-CBC + HMAC-SHA256) keyed from any secret string via SHA-256."""

    def __init__(self, secret: str):
        if not secret:
            raise ValueError("secret key is empty")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, blob: bytes) -> str:
        try:
            return self._fernet.decrypt(blob).decode("utf-8")
        except InvalidToken as exc:  # wrong key or corrupted value
            raise ValueError("cannot decrypt token: wrong EVEDM_SECRET_KEY?") from exc


_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_WEBHOOK = re.compile(r"(?i)(discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/)[A-Za-z0-9_\-.]+")
_LONG_SECRET = re.compile(r"(?i)((?:refresh_token|access_token|client_secret|code)[\"'=: ]+)([A-Za-z0-9_\-+/=.]{12,})")


def redact(text: str) -> str:
    """Mask JWTs and token-looking values (TOK-05, IMP-09)."""
    text = _JWT.sub("<jwt:redacted>", text)
    text = _WEBHOOK.sub(lambda m: m.group(1) + "<redacted>", text)
    return _LONG_SECRET.sub(lambda m: m.group(1) + "<redacted>", text)


def tail(value: str, n: int = 4) -> str:
    """Last n characters, for identification in the UI (CHR-01)."""
    return "…" + value[-n:] if value else ""
