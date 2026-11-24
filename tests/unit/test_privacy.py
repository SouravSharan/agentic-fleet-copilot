"""Unit tests for tenant-scoped HKDF+HMAC pseudonymisation and ops log redaction."""

from __future__ import annotations

import pytest

from fleet_copilot.privacy import MissingPseudonymKeyError, redact_for_logs, subject_ref

FIXED_KEY = "a1" * 32


def test_subject_ref_is_deterministic_tenant_scoped_and_hides_raw_id() -> None:
    ref1 = subject_ref("acme-001", "employee", "emp-acme-1001", key=FIXED_KEY)
    ref2 = subject_ref("acme-001", "employee", "emp-acme-1001", key=FIXED_KEY)
    ref_other_tenant = subject_ref("globex-002", "employee", "emp-acme-1001", key=FIXED_KEY)
    ref_other_kind = subject_ref("acme-001", "hostname", "emp-acme-1001", key=FIXED_KEY)

    assert ref1 == ref2
    assert ref1.startswith("subj_")
    assert len(ref1) == len("subj_") + 16
    assert ref1 != ref_other_tenant
    assert ref1 != ref_other_kind
    assert "emp-acme-1001" not in ref1
    assert "acme" not in ref1


def test_subject_ref_fails_fast_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from fleet_copilot.config import get_settings

    monkeypatch.setenv("PSEUDONYM_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(MissingPseudonymKeyError):
            subject_ref("acme-001", "employee", "emp-acme-1001")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_redact_for_logs_masks_ip_ipv6_mac_and_email() -> None:
    raw = (
        "Failed from 192.168.4.28 and fe80::1ff:fe23:4567:890a "
        "mac 84:1b:a2:38:73:f1 user alice.smith@acme.example.com"
    )
    redacted = redact_for_logs(raw)
    assert "192.168.4.28" not in redacted
    assert "fe80::1ff:fe23:4567:890a" not in redacted
    assert "84:1b:a2:38:73:f1" not in redacted
    assert "alice.smith@acme.example.com" not in redacted
    assert "[REDACTED_IP]" in redacted
    assert "[REDACTED_MAC]" in redacted
    assert "[REDACTED_EMAIL]" in redacted


def test_log_filter_strips_session_signing_key_and_demo_secrets() -> None:
    import logging

    from fleet_copilot.observability.logging import DISALLOWED_LOG_KEYS, PrivacyRedactionFilter

    assert "session_signing_key" in DISALLOWED_LOG_KEYS
    filt = PrivacyRedactionFilter()
    rec = logging.LogRecord(
        name="fleet_copilot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="auth initialized",
        args=(),
        exc_info=None,
    )
    rec.session_signing_key = "super-secret-signing-key"
    rec.demo_access_codes = "secret-code:rev_01"
    assert filt.filter(rec) is True
    assert not hasattr(rec, "session_signing_key")
    assert not hasattr(rec, "demo_access_codes")
