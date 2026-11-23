"""Unit tests for all five trend analyzers and rollup using synthetic in-memory telemetry."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fleet_copilot.domain.models import (
    ComplianceRecord,
    DeviceState,
    PolicyValue,
    SnapshotMetricPoint,
    SoftwareRecord,
    TenantContext,
)
from fleet_copilot.services.trends.battery import BatteryAnalyzer
from fleet_copilot.services.trends.compliance import ComplianceDriftAnalyzer
from fleet_copilot.services.trends.disk import DiskAnalyzer
from fleet_copilot.services.trends.ram import RamAnalyzer
from fleet_copilot.services.trends.rollup import build_device_rollup
from fleet_copilot.services.trends.software import SoftwareAnalyzer

AS_OF = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)


def _ctx() -> TenantContext:
    return TenantContext(
        company_id="acme-001",
        actor_id="admin-1",
        role="it_admin",
        trace_id="trace-synth",
    )


def _default_policies() -> dict[str, PolicyValue]:
    return {
        "battery_eol_cycles": PolicyValue(value=800, source="global"),
        "battery_capacity_drop_pct": PolicyValue(value=3.0, source="global"),
        "high_ram_used_pct": PolicyValue(value=80.0, source="global"),
        "ram_sustained_ratio": PolicyValue(value=0.7, source="global"),
        "low_disk_free_pct": PolicyValue(value=15.0, source="global"),
        "disk_burn_min_slope_pct_per_day": PolicyValue(value=0.1, source="global"),
        "disk_trend_min_r2": PolicyValue(value=0.8, source="global"),
        "software_baseline_min_share": PolicyValue(value=0.8, source="global"),
        "flagged_software_list": PolicyValue(
            value=["uTorrent", "TeamViewer", "CleanMyMac X"],
            source="global",
        ),
        "replacement_min_signals": PolicyValue(value=2, source="global"),
    }


def _dev(
    device_id: str,
    *,
    form_factor: str = "laptop",
    ever_had_battery: bool = True,
    battery_condition: str | None = "Normal",
    employee_id: str = "emp-1",
) -> DeviceState:
    return DeviceState(
        snapshot_id=f"snap_{device_id}_last",
        company_id="acme-001",
        device_id=device_id,
        employee_id=employee_id,
        collected_at=AS_OF,
        agent_version="42.0.0",
        os_platform="darwin",
        os_product_name="macOS",
        os_product_version="15.3",
        os_hostname=f"host-{device_id}",
        model_name="MacBook Pro" if form_factor == "laptop" else "Mac mini",
        model_identifier="Mac14,5" if form_factor == "laptop" else "Mac14,3",
        ram_total_gb=16.0,
        ram_used_pct=50.0,
        disk_free_pct=40.0,
        disk_total_gb=512.0,
        has_battery_block=ever_had_battery,
        battery_present=ever_had_battery,
        battery_condition=battery_condition if ever_had_battery else None,
        first_seen_at=AS_OF - timedelta(days=29),
        last_seen_at=AS_OF,
        snapshot_count=30,
        ever_had_battery=ever_had_battery,
        form_factor=form_factor,  # type: ignore[arg-type]
    )


class _FakeRepo:
    def __init__(
        self,
        *,
        devices: list[DeviceState],
        points: list[SnapshotMetricPoint] | None = None,
        compliance_rows: list[ComplianceRecord] | None = None,
        software_rows: list[SoftwareRecord] | None = None,
    ) -> None:
        self._devices = devices
        self._points = points or []
        self._compliance = compliance_rows or []
        self._software = software_rows or []

    def tenant_as_of(self, company_id: str) -> datetime:
        return AS_OF

    def latest_states(self, company_id: str) -> list[DeviceState]:
        return list(self._devices)

    def series(
        self,
        company_id: str,
        since: datetime,
        device_ids: Sequence[str] | None = None,
    ) -> list[SnapshotMetricPoint]:
        return [
            p
            for p in self._points
            if p.collected_at >= since and (device_ids is None or p.device_id in device_ids)
        ]

    def compliance(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[ComplianceRecord]:
        return [
            c
            for c in self._compliance
            if (since is None or c.collected_at >= since)
            and (device_ids is None or c.device_id in device_ids)
        ]

    def software(
        self,
        company_id: str,
        *,
        since: datetime | None = None,
        latest_only: bool = False,
        device_ids: Sequence[str] | None = None,
    ) -> list[SoftwareRecord]:
        return [
            s
            for s in self._software
            if (since is None or s.collected_at >= since)
            and (device_ids is None or s.device_id in device_ids)
        ]

    def resolve_device(self, company_id: str, identifier: str) -> DeviceState | None:
        for d in self._devices:
            if d.device_id == identifier:
                return d
        return None

    def employee_devices(self, company_id: str, employee_id: str) -> list[DeviceState]:
        return [d for d in self._devices if d.employee_id == employee_id]

    def existing_snapshot_ids(self, company_id: str, ids: Sequence[str]) -> set[str]:
        return set(ids)


def test_battery_skips_desktops_tolerates_missing_rows_and_cycle_jitter() -> None:
    laptop = _dev("LAP1", battery_condition="Service Recommended")
    healthy_jitter = _dev("LAP2", battery_condition="Normal")
    desktop = _dev("DESK1", form_factor="desktop", ever_had_battery=False)

    t0 = AS_OF - timedelta(days=5)
    points = [
        # LAP1: missing row on day 1, cycle jitter 936 -> 935 on transition to Service Recommended
        SnapshotMetricPoint(
            snapshot_id="s1",
            device_id="LAP1",
            employee_id="emp-1",
            hostname="host-LAP1",
            collected_at=t0,
            has_battery_block=True,
            battery_present=True,
            battery_condition="Normal",
            battery_cycle_count=934,
            battery_full_charge_capacity=4000,
        ),
        SnapshotMetricPoint(
            snapshot_id="s2",
            device_id="LAP1",
            employee_id="emp-1",
            hostname="host-LAP1",
            collected_at=t0 + timedelta(days=1),
            has_battery_block=False,
        ),
        SnapshotMetricPoint(
            snapshot_id="s3",
            device_id="LAP1",
            employee_id="emp-1",
            hostname="host-LAP1",
            collected_at=t0 + timedelta(days=2),
            has_battery_block=True,
            battery_present=True,
            battery_condition="Normal",
            battery_cycle_count=936,
            battery_full_charge_capacity=3950,
        ),
        SnapshotMetricPoint(
            snapshot_id="s4",
            device_id="LAP1",
            employee_id="emp-1",
            hostname="host-LAP1",
            collected_at=t0 + timedelta(days=3),
            has_battery_block=True,
            battery_present=True,
            battery_condition="Service Recommended",
            battery_cycle_count=935,
            battery_full_charge_capacity=3820,
        ),
        SnapshotMetricPoint(
            snapshot_id="s5",
            device_id="LAP1",
            employee_id="emp-1",
            hostname="host-LAP1",
            collected_at=t0 + timedelta(days=5),
            has_battery_block=True,
            battery_present=True,
            battery_condition="Service Recommended",
            battery_cycle_count=940,
            battery_full_charge_capacity=3800,
        ),
        # LAP2: spikes to 805 mid-window, then settles at 790 Normal (latest < 800 -> NOT flagged)
        SnapshotMetricPoint(
            snapshot_id="s20",
            device_id="LAP2",
            employee_id="emp-1",
            hostname="host-LAP2",
            collected_at=t0,
            has_battery_block=True,
            battery_present=True,
            battery_condition="Normal",
            battery_cycle_count=805,
            battery_full_charge_capacity=5000,
        ),
        SnapshotMetricPoint(
            snapshot_id="s21",
            device_id="LAP2",
            employee_id="emp-1",
            hostname="host-LAP2",
            collected_at=t0 + timedelta(days=5),
            has_battery_block=True,
            battery_present=True,
            battery_condition="Normal",
            battery_cycle_count=790,
            battery_full_charge_capacity=5000,
        ),
        # DESK1: desktop with no battery
        SnapshotMetricPoint(
            snapshot_id="s30",
            device_id="DESK1",
            employee_id="emp-1",
            hostname="host-DESK1",
            collected_at=t0,
            has_battery_block=False,
        ),
    ]

    repo = _FakeRepo(devices=[laptop, healthy_jitter, desktop], points=points)
    out = BatteryAnalyzer().analyze(_ctx(), repo, _default_policies(), AS_OF)

    assert [i.device_id for i in out.insights] == ["LAP1"]
    ins = out.insights[0]
    assert ins.severity == "high"
    assert ins.stats["cycles_first"] == 934
    assert ins.stats["cycles_last"] == 940
    assert ins.stats["cycles_max"] == 940
    assert ins.stats["drop_pct"] == 5.0
    assert {e.snapshot_id for e in ins.evidence} == {"s1", "s4", "s5"}
    assert out.coverage.evaluated == 2
    assert any("missing battery" in n.lower() for n in out.coverage.notes)


def test_ram_ratio_boundary_exact_0_7() -> None:
    d_hit = _dev("RAM_HIT")
    d_miss = _dev("RAM_MISS")
    t0 = AS_OF - timedelta(days=9)

    points: list[SnapshotMetricPoint] = []
    # 7 of 10 snapshots >= 80 -> ratio = 0.70 exact -> flagged
    for i in range(10):
        points.append(
            SnapshotMetricPoint(
                snapshot_id=f"rh_{i}",
                device_id="RAM_HIT",
                employee_id="emp-1",
                hostname="host-RAM_HIT",
                collected_at=t0 + timedelta(days=i),
                ram_used_pct=80.0 if i < 7 else 50.0,
            )
        )
    # 6 of 10 snapshots >= 80 -> ratio = 0.60 -> NOT flagged
    for i in range(10):
        points.append(
            SnapshotMetricPoint(
                snapshot_id=f"rm_{i}",
                device_id="RAM_MISS",
                employee_id="emp-1",
                hostname="host-RAM_MISS",
                collected_at=t0 + timedelta(days=i),
                ram_used_pct=85.0 if i < 6 else 50.0,
            )
        )

    out = RamAnalyzer().analyze(
        _ctx(), _FakeRepo(devices=[d_hit, d_miss], points=points), _default_policies(), AS_OF
    )
    assert [i.device_id for i in out.insights] == ["RAM_HIT"]
    assert out.insights[0].severity == "medium"
    assert out.insights[0].stats["share"] == 0.7


def test_disk_perfect_line_noisy_flat_and_r2_gate() -> None:
    d_drain = _dev("DISK_DRAIN")
    d_flat_noisy = _dev("DISK_NOISY")
    d_low_r2 = _dev("DISK_LOW_R2")
    t0 = AS_OF - timedelta(days=9)

    points: list[SnapshotMetricPoint] = []
    # Perfect line: 30% -> 21% at -1.0%/day (not yet <15%, but depleting with r2=1.0)
    for i in range(10):
        points.append(
            SnapshotMetricPoint(
                snapshot_id=f"dd_{i}",
                device_id="DISK_DRAIN",
                employee_id="emp-1",
                hostname="host-DISK_DRAIN",
                collected_at=t0 + timedelta(days=i),
                disk_free_pct=30.0 - float(i),
            )
        )
    # Noisy flat around 50% -> NOT flagged
    noisy_vals = [50.0, 53.0, 48.0, 52.0, 49.0, 51.0, 47.0, 52.0, 49.0, 50.0]
    for i, val in enumerate(noisy_vals):
        points.append(
            SnapshotMetricPoint(
                snapshot_id=f"dn_{i}",
                device_id="DISK_NOISY",
                employee_id="emp-1",
                hostname="host-DISK_NOISY",
                collected_at=t0 + timedelta(days=i),
                disk_free_pct=val,
            )
        )
    # Negative slope < -0.1 but poor R^2 (< 0.8) and latest > 15% -> blocked by R^2 gate
    low_r2_vals = [40.0, 20.0, 45.0, 22.0, 42.0, 18.0, 38.0, 17.0, 35.0, 25.0]
    for i, val in enumerate(low_r2_vals):
        points.append(
            SnapshotMetricPoint(
                snapshot_id=f"dl_{i}",
                device_id="DISK_LOW_R2",
                employee_id="emp-1",
                hostname="host-DISK_LOW_R2",
                collected_at=t0 + timedelta(days=i),
                disk_free_pct=val,
            )
        )

    out = DiskAnalyzer().analyze(
        _ctx(),
        _FakeRepo(devices=[d_drain, d_flat_noisy, d_low_r2], points=points),
        _default_policies(),
        AS_OF,
    )
    assert [i.device_id for i in out.insights] == ["DISK_DRAIN"]
    ins = out.insights[0]
    assert ins.stats["slope_pct_per_day"] == -1.0
    assert ins.stats["r2"] == 1.0
    assert ins.stats["days_to_low"] == 6.0
    assert ins.stats["days_to_zero"] == 21.0


def test_compliance_recovery_and_chronic() -> None:
    d_rec = _dev("COMP_REC")
    d_chr = _dev("COMP_CHR")
    t0 = AS_OF - timedelta(days=3)

    rows = [
        # COMP_REC: pass -> fail -> pass (drift + recovered)
        ComplianceRecord(
            snapshot_id="c1",
            device_id="COMP_REC",
            employee_id="emp-1",
            hostname="host-COMP_REC",
            collected_at=t0,
            check_id="screen_lock",
            status="pass",
            severity="medium",
        ),
        ComplianceRecord(
            snapshot_id="c2",
            device_id="COMP_REC",
            employee_id="emp-1",
            hostname="host-COMP_REC",
            collected_at=t0 + timedelta(days=1),
            check_id="screen_lock",
            status="fail",
            severity="medium",
        ),
        ComplianceRecord(
            snapshot_id="c3",
            device_id="COMP_REC",
            employee_id="emp-1",
            hostname="host-COMP_REC",
            collected_at=t0 + timedelta(days=2),
            check_id="screen_lock",
            status="pass",
            severity="medium",
        ),
        # COMP_CHR: fail -> fail -> fail (chronic, NOT drift)
        ComplianceRecord(
            snapshot_id="k1",
            device_id="COMP_CHR",
            employee_id="emp-1",
            hostname="host-COMP_CHR",
            collected_at=t0,
            check_id="disk_encryption",
            status="fail",
            severity="high",
        ),
        ComplianceRecord(
            snapshot_id="k2",
            device_id="COMP_CHR",
            employee_id="emp-1",
            hostname="host-COMP_CHR",
            collected_at=t0 + timedelta(days=1),
            check_id="disk_encryption",
            status="fail",
            severity="high",
        ),
    ]

    out = ComplianceDriftAnalyzer().analyze(
        _ctx(),
        _FakeRepo(devices=[d_rec, d_chr], compliance_rows=rows),
        _default_policies(),
        AS_OF,
    )
    drifts = [i for i in out.insights if i.category == "compliance_drift"]
    chronics = [i for i in out.insights if i.category == "compliance_chronic"]

    assert len(drifts) == 1
    assert drifts[0].device_id == "COMP_REC"
    assert drifts[0].stats["recovered"] is True
    assert drifts[0].stats["kind"] == "drift"

    assert len(chronics) == 1
    assert chronics[0].device_id == "COMP_CHR"
    assert chronics[0].stats["kind"] == "chronic"
    assert chronics[0].severity == "high"
    assert any(a["type"] == "daily_compliance_pass_rate" for a in out.artifacts)


def test_software_baseline_and_first_seen() -> None:
    devs = [_dev(f"SW{i}") for i in range(5)]
    t0 = AS_OF - timedelta(days=4)
    t1 = AS_OF - timedelta(days=1)

    sw_rows: list[SoftwareRecord] = []
    # Baseline app on 5/5 devices at t0 (100% >= 80%)
    for d in devs:
        sw_rows.append(
            SoftwareRecord(
                snapshot_id=f"{d.device_id}_0",
                device_id=d.device_id,
                employee_id="emp-1",
                hostname=d.os_hostname,
                collected_at=t0,
                software_name="Slack",
                version="4.0",
                publisher="Slack",
            )
        )
    # Non-baseline flagged app installed on SW0 at t1 + non-flagged unusual app on SW1
    sw_rows.append(
        SoftwareRecord(
            snapshot_id="SW0_1",
            device_id="SW0",
            employee_id="emp-1",
            hostname="host-SW0",
            collected_at=t1,
            software_name="uTorrent",
            version="1.0",
            publisher="BitTorrent",
        )
    )
    sw_rows.append(
        SoftwareRecord(
            snapshot_id="SW1_1",
            device_id="SW1",
            employee_id="emp-1",
            hostname="host-SW1",
            collected_at=t1,
            software_name="RandomTool",
            version="0.9",
            publisher="Unknown",
        )
    )

    out = SoftwareAnalyzer().analyze(
        _ctx(),
        _FakeRepo(devices=devs, software_rows=sw_rows),
        _default_policies(),
        AS_OF,
    )
    by_dev = {i.device_id: i for i in out.insights}
    assert set(by_dev.keys()) == {"SW0", "SW1"}
    assert by_dev["SW0"].severity == "high"
    assert by_dev["SW0"].stats["first_seen_date"] == t1.date().isoformat()
    assert by_dev["SW1"].severity == "medium"


def test_rollup_counts_and_replacement_flag() -> None:
    d_sr = _dev("D_SR", battery_condition="Service Recommended")
    d_multi = _dev("D_MULTI", battery_condition="Normal")
    d_one = _dev("D_ONE", battery_condition="Normal")
    d_clean = _dev("D_CLEAN", battery_condition="Normal")

    t0 = AS_OF - timedelta(days=5)
    points = [
        SnapshotMetricPoint(
            snapshot_id="b1",
            device_id="D_SR",
            employee_id="emp-1",
            hostname="host-D_SR",
            collected_at=t0,
            has_battery_block=True,
            battery_present=True,
            battery_condition="Service Recommended",
            battery_cycle_count=1000,
            battery_full_charge_capacity=4000,
        ),
        SnapshotMetricPoint(
            snapshot_id="m1",
            device_id="D_MULTI",
            employee_id="emp-1",
            hostname="host-D_MULTI",
            collected_at=t0,
            ram_used_pct=92.0,
            disk_free_pct=3.0,
        ),
        SnapshotMetricPoint(
            snapshot_id="o1",
            device_id="D_ONE",
            employee_id="emp-1",
            hostname="host-D_ONE",
            collected_at=t0,
            ram_used_pct=90.0,
            disk_free_pct=50.0,
        ),
    ]
    repo = _FakeRepo(devices=[d_sr, d_multi, d_one, d_clean], points=points)
    policies = _default_policies()
    insights = (
        BatteryAnalyzer().analyze(_ctx(), repo, policies, AS_OF).insights
        + RamAnalyzer().analyze(_ctx(), repo, policies, AS_OF).insights
        + DiskAnalyzer().analyze(_ctx(), repo, policies, AS_OF).insights
    )

    rows = build_device_rollup([d_sr, d_multi, d_one, d_clean], insights, policies)
    by_dev = {r["device_id"]: r for r in rows}

    assert rows[0]["device_id"] == "D_MULTI"
    assert by_dev["D_MULTI"]["signal_count"] == 2
    assert by_dev["D_MULTI"]["replacement_candidate"] is True
    assert by_dev["D_SR"]["signal_count"] == 1
    assert by_dev["D_SR"]["replacement_candidate"] is True
    assert by_dev["D_ONE"]["signal_count"] == 1
    assert by_dev["D_ONE"]["replacement_candidate"] is False
    assert by_dev["D_CLEAN"]["signal_count"] == 0
    assert by_dev["D_CLEAN"]["replacement_candidate"] is False
