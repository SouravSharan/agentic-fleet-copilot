"""Integration privacy scan: verify zero PII, hostnames, IPs, MACs, or forbidden keys in audit."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fleet_copilot.clock import FixedClock
from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.domain.evidence import EvidenceLedger
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService, DraftInput
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import FORBIDDEN_AUDIT_FIELD_NAMES, AuditService
from fleet_copilot.services.fleet import get_device_details
from fleet_copilot.services.policy import PolicyService
from fleet_copilot.services.trends import analyze_fleet_trends
from scripts.db_setup import run_setup
from scripts.telemetry_sim import reset_dataset

DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "device-telemetry-dataset.ndjson"
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_MAC_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_EMP_RE = re.compile(r"\bemp-[a-z0-9-]+\b", re.IGNORECASE)


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)
    reset_dataset(writer_url=settings.telemetry_writer_database_url)

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as manager:
        yield manager


def test_audit_payloads_contain_zero_pii_hostnames_or_forbidden_keys(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    repo = PgTelemetryRepository(db_manager)
    store = PgCopilotStore(db_manager)
    policy_svc = PolicyService(store)
    audit_svc = AuditService(db_manager)
    svc = ActionService(db_manager, repo, store, policy_svc, audit_svc, MockAdapter())

    clock = FixedClock(datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC))
    ctx = TenantContext(
        company_id="acme-001",
        actor_id="acme_admin",
        role="it_admin",
        trace_id="trace-privacy-scan",
    )
    thread_id = "acme-001:privacy-thread"
    turn_id = "turn-privacy-1"
    store.ensure_thread(ctx.company_id, thread_id, ctx.actor_id)
    store.ensure_turn(ctx.company_id, thread_id, turn_id, ctx.trace_id, ctx.actor_id, "scan")

    ledger = EvidenceLedger()
    for ev in analyze_fleet_trends(ctx, ["all"], repo, policy_svc).evidence:
        ledger.add(ev)
    for dev in repo.latest_states("acme-001"):
        for ev in get_device_details(ctx, dev.device_id, repo, policy_svc).evidence:
            ledger.add(ev)

    def _h(device_id: str, prefix: str = "") -> list[str]:
        return [
            ev.handle
            for ev in ledger.all()
            if ev.handle is not None
            and ev.device_id == device_id
            and (not prefix or ev.metric.startswith(prefix))
        ]

    # 1. Run all 4 lifecycle cases
    lifecycles = [
        DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id="M4XVHUV1MEPZ",
            arguments={"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB DDR5"},
            rationale="Sustained RAM",
            evidence_handles=_h("M4XVHUV1MEPZ", "ram_used_pct"),
        ),
        DraftInput(
            tool_name="open_remediation_ticket",
            target_type="device",
            target_id="1LYSSFD074BB",
            arguments={
                "device_id": "1LYSSFD074BB",
                "check_id": "screen_lock",
                "note": "Contact user alice@acme.example.com at 192.168.4.28",
            },
            rationale="screen_lock fail",
            evidence_handles=_h("1LYSSFD074BB", "compliance:screen_lock"),
        ),
        DraftInput(
            tool_name="flag_device_for_replacement",
            target_type="device",
            target_id="8NM23J95R5I6",
            arguments={"device_id": "8NM23J95R5I6", "reason": "Replace acme-thinkpad-8"},
            rationale="Multi-signal",
            evidence_handles=_h("8NM23J95R5I6", "battery_"),
        ),
        DraftInput(
            tool_name="notify_employee",
            target_type="employee",
            target_id="emp-acme-1003",
            arguments={
                "employee_id": "emp-acme-1003",
                "message": "Your laptop MT7PJB7N5LRE (acme-macbook-4) is low on disk space.",
            },
            rationale="Low disk",
            evidence_handles=_h("MT7PJB7N5LRE", "disk_free_pct"),
        ),
    ]
    for draft_in in lifecycles:
        st = svc.stage(ctx, [draft_in], thread_id, turn_id, ledger, clock)
        assert len(st.staged) == 1
        pid = st.staged[0]["proposal_id"]
        assert svc.decide(ctx, pid, "approve", "queue", clock).applied is True
        assert svc.execute(ctx, pid, clock).status == "executed"

    # 2. Run 2 refusals (including an employee notification refusal)
    svc.precheck(
        ctx,
        DraftInput(
            tool_name="create_upgrade_order",
            target_type="device",
            target_id="DLT35MHGTH2D",
            arguments={"device_id": "DLT35MHGTH2D", "component": "battery", "spec": "Battery"},
            rationale="Desktop battery",
            evidence_handles=_h("DLT35MHGTH2D"),
        ),
        ledger,
        clock,
    )
    svc.precheck(
        ctx,
        DraftInput(
            tool_name="notify_employee",
            target_type="employee",
            target_id="emp-acme-1003",
            arguments={
                "employee_id": "emp-acme-1003",
                "message": "Check MT7PJB7N5LRE and M4XVHUV1MEPZ",
            },
            rationale="Cross-employee refusal",
            evidence_handles=_h("MT7PJB7N5LRE", "disk_free_pct"),
        ),
        ledger,
        clock,
    )

    # 3. Run 1 expiry
    exp_stage = svc.stage(
        ctx,
        [
            DraftInput(
                tool_name="open_remediation_ticket",
                target_type="device",
                target_id="MT7PJB7N5LRE",
                arguments={
                    "device_id": "MT7PJB7N5LRE",
                    "check_id": "screen_lock",
                    "note": "Fix screen lock",
                },
                rationale="Will expire",
                evidence_handles=_h("MT7PJB7N5LRE", "compliance:screen_lock"),
            )
        ],
        thread_id,
        turn_id,
        ledger,
        clock,
    )
    assert len(exp_stage.staged) == 1
    clock.advance(timedelta(hours=73))
    assert len(svc.expire_due(ctx, clock)) == 1

    # 4. Scan EVERY audit_logs.payload_json row!
    raw_rows = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()]
    dataset_hostnames = {
        r["os"]["hostname"]
        for r in raw_rows
        if isinstance(r.get("os"), dict) and r["os"].get("hostname")
    }

    audit_rows = audit_svc.list(ctx, limit=500)
    assert len(audit_rows) >= 16

    def _walk_keys(obj: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.add(str(k))
                keys |= _walk_keys(v)
        elif isinstance(obj, list):
            for item in obj:
                keys |= _walk_keys(item)
        return keys

    for row in audit_rows:
        payload = row["payload_json"]
        serialized = json.dumps(payload)
        assert not _IPV4_RE.search(serialized), f"IPv4 in audit row {row}"
        assert not _MAC_RE.search(serialized), f"MAC in audit row {row}"
        assert not _EMAIL_RE.search(serialized), f"Email in audit row {row}"
        assert not _EMP_RE.search(serialized), f"Raw emp- ID in audit row {row}"
        for host in dataset_hostnames:
            assert host not in serialized, f"Hostname {host!r} leaked in audit row {row}"
        overlap = FORBIDDEN_AUDIT_FIELD_NAMES & _walk_keys(payload)
        assert not overlap, f"Forbidden key(s) {overlap} in audit row {row}"

    # 5. Scan integration_outbox.payload_json for network/hardware PII leaks
    outbox_rows = store.list_outbox("acme-001")
    assert len(outbox_rows) == 4
    for row in outbox_rows:
        serialized = json.dumps(row["payload_json"])
        assert not _IPV4_RE.search(serialized), f"IPv4 leaked in outbox {row}"
        assert not _MAC_RE.search(serialized), f"MAC leaked in outbox {row}"
        assert not _EMAIL_RE.search(serialized), f"Email leaked in outbox {row}"
        assert "hardware_uuid" not in serialized
        assert "serial_number" not in serialized

    assert audit_svc.verify(ctx)["ok"] is True
