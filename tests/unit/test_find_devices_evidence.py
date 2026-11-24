"""Unit tests for `find_devices` multi-filter evidence and `Answer`/`Plan` schema descriptions."""

from __future__ import annotations

from fleet_copilot.agent.schemas import Answer, Claim, Plan, PlanStep
from fleet_copilot.domain.models import DeviceFilters, TenantContext
from fleet_copilot.services.fleet import find_devices
from fleet_copilot.services.policy import PolicyService
from tests.graph.fakes import InMemoryCopilotStore, InMemoryTelemetryRepo


def test_find_devices_emits_evidence_for_every_matched_filter_category() -> None:
    repo = InMemoryTelemetryRepo()
    store = InMemoryCopilotStore()
    policy_svc = PolicyService(store)
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="rev_01:acme_admin",
        role="it_admin",
        trace_id="test-multi-ev",
    )

    # Combined low_disk + macOS filter must emit BOTH disk_free_pct AND os_version evidence
    res = find_devices(
        ctx,
        DeviceFilters(low_disk_only=True, os_family="macos", os_version_lt="15"),
        repo,
        policy_svc,
    )
    assert res.ok
    assert res.coverage is not None and res.coverage.matched >= 1
    by_dev: dict[str, set[str]] = {}
    for ev in res.evidence:
        by_dev.setdefault(ev.device_id, set()).add(ev.metric)
    for dev_id, metrics in by_dev.items():
        assert "disk_free_pct" in metrics, f"{dev_id} missing disk_free_pct"
        assert "os_version" in metrics, f"{dev_id} missing os_version"

    # Unfiltered / form-factor-only lookup falls back cleanly to device_state
    plain = find_devices(ctx, DeviceFilters(form_factor="laptop"), repo, policy_svc)
    assert plain.ok
    assert plain.evidence
    assert all(ev.metric == "device_state" for ev in plain.evidence)


def test_plan_and_answer_schemas_have_clean_docstrings_and_field_descriptions() -> None:
    for cls in (PlanStep, Plan, Claim, Answer):
        doc = cls.__doc__ or ""
        assert "\u00a7" not in doc
        schema = cls.model_json_schema()
        props = schema.get("properties") or {}
        assert props, f"{cls.__name__} schema has no properties"
        for field_name, field_schema in props.items():
            assert field_schema.get("description"), (
                f"{cls.__name__}.{field_name} is missing Field(description=...)"
            )
