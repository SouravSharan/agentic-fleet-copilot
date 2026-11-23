"""JSON ops logging with disallowed-key stripping, PII redaction, and trace_id binding."""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from fleet_copilot.privacy import redact_for_logs

_TRACE_ID_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

DISALLOWED_LOG_KEYS: frozenset[str] = frozenset(
    {
        "prompt",
        "answer",
        "user_message",
        "assistant_response",
        "response_json",
        "chart_data",
        "message_text",
        "text",
        "content",
        "hostname",
        "employee_id",
        "api_key",
        "gemini_api_key",
        "pseudonym_key",
        "langgraph_aes_key",
        "password",
        "secret",
        "token",
        "authorization",
    }
)

_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def current_trace_id() -> str:
    """Return the active trace_id bound in the current contextvar."""
    return _TRACE_ID_VAR.get()


@contextmanager
def bind_trace_id(trace_id: str) -> Iterator[None]:
    """Bind `trace_id` into logging context for the duration of a request/turn."""
    tok = _TRACE_ID_VAR.set(trace_id)
    try:
        yield
    finally:
        _TRACE_ID_VAR.reset(tok)


class PrivacyRedactionFilter(logging.Filter):
    """Strips disallowed PII/prompt/secret keys and redacts IPs/MACs/emails in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "trace_id", None):
            record.trace_id = current_trace_id()

        # Drop disallowed custom keys attached via `extra=...`
        for key in list(record.__dict__.keys()):
            if key.lower() in DISALLOWED_LOG_KEYS:
                del record.__dict__[key]

        # Redact the message string and any string extra fields
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001
            rendered = str(record.msg)
        record.msg = redact_for_logs(rendered)
        record.args = None

        for key, val in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_ATTRS:
                continue
            if isinstance(val, str):
                record.__dict__[key] = redact_for_logs(val)
        return True


class JsonLogFormatter(logging.Formatter):
    """Single-line JSON formatter emitting safe operational fields only."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", "") or current_trace_id(),
            "msg": redact_for_logs(record.getMessage()),
        }
        for key, val in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in payload:
                continue
            if key.lower() in DISALLOWED_LOG_KEYS:
                continue
            if isinstance(val, (str, int, float, bool)) or val is None:
                payload[key] = redact_for_logs(val) if isinstance(val, str) else val
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_class"] = record.exc_info[0].__name__
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def configure_logging(*, log_format: str = "json", log_level: str = "INFO") -> logging.Logger:
    """Configure the `fleet_copilot` logger with privacy filtering and JSON/text formatting."""
    logger = logging.getLogger("fleet_copilot")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.filters.clear()

    handler = logging.StreamHandler()
    privacy_filter = PrivacyRedactionFilter()
    handler.addFilter(privacy_filter)
    logger.addFilter(privacy_filter)

    if log_format.lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s")
        )
    logger.addHandler(handler)
    logger.propagate = True
    return logger
