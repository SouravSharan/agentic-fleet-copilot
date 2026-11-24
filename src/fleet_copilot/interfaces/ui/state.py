"""Session state helpers, tenant-safe context creation, error boundary, and perf tracking."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import streamlit as st

from fleet_copilot.domain.models import TenantContext
from fleet_copilot.services.demo_auth import DemoSession, to_tenant_context

logger = logging.getLogger(__name__)

SESSION_KEY = "demo_session"
AUTH_ATTEMPTS_KEY = "auth_failed_attempts"
MAX_AUTH_ATTEMPTS = 10

# Session keys cleared whenever the user signs out or switches personas
_TENANT_SCOPED_KEYS: tuple[str, ...] = (
    "active_thread_id",
    "selected_turn_id",
    "inspector_open",
    "pending_prompt",
    "pending_auto_send",
    "pending_new_chat",
    "active_job_id",
    "active_job_prompt",
    "active_job_thread_id",
    "pinned_chart_ids",
    "fixture_decisions",
    "thread_list_version",
    "thread_search_query",
    "renaming_thread_id",
    "last_resume_outcomes",
    "ephemeral_turns",
    "_active_turns_by_id",
    "_active_turn_order",
    "expanded_thread_ids",
    "selected_device_id",
    "fleet_device_picker",
    "_fleet_last_table_rows",
    "_active_device_summaries",
    "confirm_delete_chart_id",
    "approvals_outbox_receipt",
    "how_it_works_default_tab",
    "queue_decision_outcomes",
    "guardrail_active_job",
    "guardrail_live_result",
    "guardrail_selected_id",
    "demo_inject_last",
    "demo_reset_last_summary",
    "tour_completed_steps",
    "tour_used",
)

# Registry populated by app.py so any page can switch cleanly via `st.switch_page`
_PAGE_REGISTRY: dict[str, Any] = {}


def register_pages(pages: dict[str, Any]) -> None:
    """Store `st.Page` instances keyed by route slug for cross-page navigation."""
    _PAGE_REGISTRY.clear()
    _PAGE_REGISTRY.update(pages)


def get_page(slug: str) -> Any | None:
    """Return a registered `st.Page` by slug (`sign_in`, `home`, `copilot`, etc.)."""
    return _PAGE_REGISTRY.get(slug)


def switch_to_page(slug: str) -> None:
    """Navigate to another page by slug if registered."""
    page = get_page(slug)
    if page is not None:
        st.switch_page(page)


def get_session() -> DemoSession | None:
    """Return the authenticated `DemoSession` from `st.session_state`, if any."""
    raw = st.session_state.get(SESSION_KEY)
    if isinstance(raw, DemoSession):
        return raw
    if isinstance(raw, dict):
        try:
            sess = DemoSession.model_validate(raw)
            st.session_state[SESSION_KEY] = sess
            return sess
        except Exception:  # noqa: BLE001
            return None
    return None


def set_session(session: DemoSession) -> None:
    """Store a verified `DemoSession` and wipe any previous persona's tenant-scoped UI state."""
    for key in _TENANT_SCOPED_KEYS:
        st.session_state.pop(key, None)
    st.session_state[SESSION_KEY] = session
    st.session_state[AUTH_ATTEMPTS_KEY] = 0


def clear_session() -> None:
    """Sign out the current demo persona and clear tenant-scoped UI state."""
    st.session_state.pop(SESSION_KEY, None)
    for key in _TENANT_SCOPED_KEYS:
        st.session_state.pop(key, None)


def require_session() -> DemoSession:
    """Ensure a page has an active `DemoSession`, redirecting to Sign in otherwise."""
    sess = get_session()
    if sess is None:
        sign_in_page = get_page("sign_in")
        if sign_in_page is not None:
            st.switch_page(sign_in_page)
        st.info("Please sign in to continue.")
        st.stop()
    return sess


def new_trace_id() -> str:
    """Generate a unique trace ID for an interactive UI action."""
    return f"ui-{uuid.uuid4().hex[:12]}"


def make_tenant_context(session: DemoSession, *, trace_id: str | None = None) -> TenantContext:
    """Build a `TenantContext` solely from `demo_auth.to_tenant_context`."""
    return to_tenant_context(session, trace_id or new_trace_id())


def queue_copilot_prompt(
    prompt: str,
    *,
    auto_send: bool = True,
    new_chat: bool = True,
) -> None:
    """Hand off a prompt from Home or Guided Tour to the Copilot page."""
    st.session_state["pending_prompt"] = prompt.strip()
    st.session_state["pending_auto_send"] = bool(auto_send)
    st.session_state["pending_new_chat"] = bool(new_chat)
    if new_chat:
        st.session_state["active_thread_id"] = None
        st.session_state["selected_turn_id"] = None


def open_copilot_thread(thread_id: str) -> None:
    """Select an actor-owned thread and switch to Copilot."""
    st.session_state["active_thread_id"] = thread_id
    st.session_state["selected_turn_id"] = None
    switch_to_page("copilot")


def mark_tour_step_done(step_num: int) -> None:
    """Record a completed guided-tour step in session state."""
    completed: set[int] = set(st.session_state.get("tour_completed_steps") or ())
    completed.add(step_num)
    st.session_state["tour_completed_steps"] = completed
    st.session_state["tour_used"] = True


def bump_thread_list_version() -> None:
    """Increment the per-session counter to refresh cached thread metadata after mutations."""
    st.session_state["thread_list_version"] = (
        int(st.session_state.get("thread_list_version", 0)) + 1
    )


def perf_mark_start(action: str) -> None:
    """Record the monotonic timestamp at the start of a UI click callback."""
    st.session_state["_perf_action"] = action
    st.session_state["_perf_t0"] = time.perf_counter()


def perf_enabled() -> bool:
    """Enable DOM perf markers when `?perf=1` is present at any point in the session."""
    if st.query_params.get("perf") == "1":
        st.session_state["_perf_enabled"] = True
    return bool(st.session_state.get("_perf_enabled", False))


def perf_finish_if_active(*, emit_marker: bool = True) -> float | None:
    """Complete an in-app rerun timing measurement and optionally emit a DOM marker (`?perf=1`)."""
    t0 = st.session_state.pop("_perf_t0", None)
    action = st.session_state.pop("_perf_action", None)
    elapsed_ms: float | None = None
    if isinstance(t0, (int, float)) and action:
        elapsed_ms = round((time.perf_counter() - float(t0)) * 1000.0, 2)
        seq = int(st.session_state.get("_perf_seq", 0)) + 1
        st.session_state["_perf_seq"] = seq
        entry = {"seq": seq, "action": str(action), "ms": elapsed_ms}
        samples: list[dict[str, Any]] = list(st.session_state.get("_perf_samples") or [])
        samples.append(entry)
        st.session_state["_perf_samples"] = samples[-200:]
        st.session_state["_perf_last"] = entry

    if emit_marker and perf_enabled():
        last = st.session_state.get("_perf_last")
        if isinstance(last, dict):
            st.markdown(
                f"<span data-fc-perf-seq='{last.get('seq', 0)}' "
                f"data-fc-perf-action='{last.get('action', '')}' "
                f"data-fc-perf-ms='{last.get('ms', 0)}'></span>",
                unsafe_allow_html=True,
            )
    return elapsed_ms


def safe_page_run(render_fn: Callable[[], None]) -> None:
    """Execute a page renderer behind a friendly error boundary (never exposes tracebacks)."""
    trace_id = new_trace_id()
    try:
        render_fn()
    except Exception as exc:  # noqa: BLE001
        # Let Streamlit's own control-flow exceptions (RerunException / StopException) propagate
        exc_mod = type(exc).__module__ or ""
        if exc_mod.startswith("streamlit.runtime.scriptrunner"):
            raise
        logger.exception(
            "ui_page_error",
            extra={"trace_id": trace_id, "error_class": type(exc).__name__},
        )
        st.error(
            f"Something went wrong. Try again. (Reference: `{trace_id}`)",
            icon=":material/error:",
        )
