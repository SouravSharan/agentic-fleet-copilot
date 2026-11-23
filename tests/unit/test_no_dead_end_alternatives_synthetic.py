"""Unit test enforcing the no-dead-end alternatives invariant across synthetic combinations."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product

from fleet_copilot.clock import FixedClock
from fleet_copilot.domain.models import Evidence, PolicyValue
from fleet_copilot.domain.policy_rules import (
    DeviceFacts,
    HardwareCatalogRow,
    ProposalDraft,
    evaluate,
)

AS_OF = datetime(2026, 6, 12, 11, 0, 0, tzinfo=UTC)
CLOCK = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))


def _policies() -> dict[str, PolicyValue]:
    return {
        "low_disk_free_pct": PolicyValue(value=15.0, source="global"),
        "high_ram_used_pct": PolicyValue(value=80.0, source="global"),
        "ram_sustained_ratio": PolicyValue(value=0.7, source="global"),
        "battery_eol_cycles": PolicyValue(value=800, source="global"),
        "battery_capacity_drop_pct": PolicyValue(value=3.0, source="global"),
        "stale_device_days": PolicyValue(value=7, source="global"),
        "max_telemetry_lag_days": PolicyValue(value=None, source="global"),
        "replacement_min_signals": PolicyValue(value=2, source="global"),
        "notify_message_max_chars": PolicyValue(value=800, source="global"),
    }


def test_every_suggested_alternative_passes_evaluate_synthetic() -> None:
    """Every alternative suggested on any synthetic refusal must itself return allow/warnings."""
    policies = _policies()
    ev = Evidence(
        handle="E1",
        snapshot_id="snap_synth",
        device_id="SYN1",
        hostname="host-SYN1",
        collected_at=AS_OF,
        metric="synthetic",
        value=1,
        label="synthetic evidence",
    )

    for ram_up, stor_up, bat_srv, ram_sust, disk_low, disk_dep in product([True, False], repeat=6):
        cat = HardwareCatalogRow(
            model_identifier="SYN-MODEL",
            model_name="Synth",
            form_factor="laptop" if bat_srv else "desktop",
            ram_upgradable=ram_up,
            ram_max_gb=64 if ram_up else None,
            storage_upgradable=stor_up,
            battery_serviceable=bat_srv,
            source_urls=["https://example.test"],
            confidence="high",
            verified_at="2026-11-23",
        )
        active = []
        if ram_sust:
            active.append("ram")
        if disk_low or disk_dep:
            active.append("disk")

        facts = DeviceFacts(
            device_id="SYN1",
            hostname="host-SYN1",
            model_identifier="SYN-MODEL",
            form_factor="laptop" if bat_srv else "desktop",
            ever_had_battery=bat_srv,
            battery_condition="Normal" if bat_srv else None,
            cycles=200 if bat_srv else None,
            ram_sustained=ram_sust,
            ram_total_gb=16.0,
            disk_latest_free_pct=2.0 if disk_low else 20.0,
            disk_depleting=disk_dep,
            days_to_low=10.0 if (disk_dep and not disk_low) else None,
            storage_total_gb=512.0,
            failing_checks=[],
            non_baseline_apps=[],
            active_signals=active,
            latest_snapshot_at=AS_OF,
        )

        drafts = [
            ProposalDraft(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="SYN1",
                arguments={"device_id": "SYN1", "component": "ram", "spec": "32 GB DDR5"},
                rationale="Upgrade RAM",
                evidence=[ev],
            ),
            ProposalDraft(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="SYN1",
                arguments={"device_id": "SYN1", "component": "storage", "spec": "1 TB SSD"},
                rationale="Upgrade SSD",
                evidence=[ev],
            ),
            ProposalDraft(
                tool_name="create_upgrade_order",
                target_type="device",
                target_id="SYN1",
                arguments={"device_id": "SYN1", "component": "battery", "spec": "OEM Battery"},
                rationale="Replace battery",
                evidence=[ev],
            ),
        ]

        for draft in drafts:
            verdict = evaluate(draft, facts, policies, cat, CLOCK, AS_OF)
            if verdict.decision == "refuse":
                for alt in verdict.alternatives:
                    alt_draft = ProposalDraft(
                        tool_name=alt.tool,  # type: ignore[arg-type]
                        target_type="device",
                        target_id="SYN1",
                        arguments=dict(alt.args_hint),
                        rationale="Follow suggested alternative",
                        evidence=[ev],
                    )
                    alt_verdict = evaluate(alt_draft, facts, policies, cat, CLOCK, AS_OF)
                    assert alt_verdict.decision in ("allow", "allow_with_warnings"), (
                        f"Dead-end alternative {alt} for {draft.arguments}: {alt_verdict.reasons}"
                    )
