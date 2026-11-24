"""Neutralize instruction-like untrusted telemetry text before it reaches any LLM."""

from __future__ import annotations

import re
from typing import Any

# Case-insensitive patterns matching prompt-injection / instruction-like text (audit/13 §3.1).
# Benign names such as "System Preferences", "Approve.app", "Microsoft Teams", "Docker Desktop"
# do NOT match any of these patterns.
SUSPICIOUS_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:system|assistant|developer)\s*:", re.IGNORECASE),
    re.compile(r"\bignore\s+(?:all|previous)\b", re.IGNORECASE),
    re.compile(r"\bapprove\s+(?:all|pending)\b", re.IGNORECASE),
    re.compile(r"\byou\s+(?:must|should)\s+now\b", re.IGNORECASE),
    re.compile(r"</?(?:system|tool)", re.IGNORECASE),
)


def is_suspicious_text(value: str) -> bool:
    """Return True if `value` matches any instruction-like prompt-injection pattern."""
    return any(pat.search(value) is not None for pat in SUSPICIOUS_TEXT_PATTERNS)


def neutralize_untrusted_text(
    value: str,
    *,
    evidence_handle: str | None = None,
) -> tuple[str, bool]:
    """Replace instruction-like text with a safe placeholder; return `(safe_text, was_withheld)`.

    Clean values are returned completely unchanged with `was_withheld=False`.
    """
    if not is_suspicious_text(value):
        return value, False
    if evidence_handle:
        return f"[untrusted text withheld, see evidence {evidence_handle}]", True
    return "[untrusted text withheld]", True


def neutralize_untrusted_value(
    obj: Any,
    *,
    evidence_handle: str | None = None,
) -> tuple[Any, bool]:
    """Recursively neutralize suspicious string values inside dicts/lists; return `(val, flagged)`.

    When nothing is flagged, returns the exact structure unchanged (no added keys or reordering).
    """
    if isinstance(obj, str):
        return neutralize_untrusted_text(obj, evidence_handle=evidence_handle)
    if isinstance(obj, list):
        out_list: list[Any] = []
        flagged = False
        for item in obj:
            cleaned, item_flag = neutralize_untrusted_value(item, evidence_handle=evidence_handle)
            flagged = flagged or item_flag
            out_list.append(cleaned)
        return out_list, flagged
    if isinstance(obj, tuple):
        out_items: list[Any] = []
        flagged = False
        for item in obj:
            cleaned, item_flag = neutralize_untrusted_value(item, evidence_handle=evidence_handle)
            flagged = flagged or item_flag
            out_items.append(cleaned)
        return tuple(out_items), flagged
    if isinstance(obj, dict):
        out_dict: dict[Any, Any] = {}
        flagged = False
        for k, v in obj.items():
            cleaned, val_flag = neutralize_untrusted_value(v, evidence_handle=evidence_handle)
            flagged = flagged or val_flag
            out_dict[k] = cleaned
        return out_dict, flagged
    return obj, False
