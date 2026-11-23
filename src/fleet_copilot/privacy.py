"""Privacy helpers: tenant-scoped HKDF-SHA256 + HMAC-SHA256 subject refs and log redaction."""

from __future__ import annotations

import hashlib
import hmac
import re

from fleet_copilot.config import get_settings

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"(?<![A-Za-z0-9:])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![A-Za-z0-9:])"
)
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


class MissingPseudonymKeyError(ValueError):
    """Raised when `PSEUDONYM_KEY` is missing or blank."""


def _hkdf_sha256(ikm: bytes, info: bytes, *, salt: bytes = b"", length: int = 32) -> bytes:
    """RFC 5869 HKDF-SHA256 using only the Python standard library."""
    hash_len = 32
    actual_salt = salt if salt else b"\x00" * hash_len
    prk = hmac.new(actual_salt, ikm, hashlib.sha256).digest()
    okm = bytearray()
    prev = b""
    counter = 1
    while len(okm) < length:
        prev = hmac.new(prk, prev + info + bytes([counter]), hashlib.sha256).digest()
        okm.extend(prev)
        counter += 1
    return bytes(okm[:length])


def subject_ref(
    company_id: str,
    kind: str,
    value: str,
    *,
    key: str | None = None,
) -> str:
    """Compute a tenant-scoped pseudonymous subject reference for audit logs.

    Formula (05 §3.9.5):
        `"subj_" + hex(HMAC-SHA256(HKDF-SHA256(key, info=company_id), f"{kind}:{value}"))[:16]`
    """
    raw_key = key
    if raw_key is None:
        secret = get_settings().pseudonym_key
        raw_key = secret.get_secret_value() if secret is not None else None
    if not raw_key or not raw_key.strip():
        raise MissingPseudonymKeyError("PSEUDONYM_KEY must be configured for pseudonymisation")

    derived = _hkdf_sha256(
        ikm=raw_key.strip().encode("utf-8"),
        info=company_id.encode("utf-8"),
        length=32,
    )
    digest = hmac.new(derived, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
    return f"subj_{digest[:16]}"


def redact_for_logs(text: str) -> str:
    """Mask MAC addresses, emails, IPv4, and IPv6 addresses in operational log strings."""
    # Redact MAC before IPv6 so colon-separated MACs get the MAC tag.
    redacted = _MAC_RE.sub("[REDACTED_MAC]", text)
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    redacted = _IPV4_RE.sub("[REDACTED_IP]", redacted)
    redacted = _IPV6_RE.sub("[REDACTED_IPV6]", redacted)
    return redacted
