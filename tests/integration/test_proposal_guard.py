"""Integration tests for copilot.proposals_guard() BEFORE UPDATE trigger."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from psycopg import errors

from fleet_copilot.config import get_settings
from fleet_copilot.db import DatabaseManager
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from scripts.db_setup import run_setup


@pytest.fixture(scope="module")
def db_manager() -> Iterator[DatabaseManager]:
    settings = get_settings()
    run_setup(admin_url=settings.admin_database_url)

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as manager:
        yield manager


def _seed_pending(db: DatabaseManager, suffix: str) -> str:
    store = PgCopilotStore(db)
    tid = f"acme-001:guard-{suffix}"
    turn_id = f"turn-{suffix}"
    prop_id = f"prop-{suffix}"
    store.ensure_thread("acme-001", tid, "acme_admin")
    store.ensure_turn("acme-001", tid, turn_id, f"trace-{suffix}", "acme_admin", "msg")
    with db.copilot_cursor("acme-001") as cur:
        cur.execute(
            """
            INSERT INTO copilot.action_proposals (
                proposal_id, batch_id, thread_id, turn_id, company_id,
                proposed_by_actor_id, tool_name, target_type, target_id,
                arguments_json, rationale, evidence_snapshot_ids,
                evidence_as_of, policy_check_json, status,
                dedupe_key, approval_hash, policy_version, idempotency_key
            )
            VALUES (
                %s, %s, %s, %s, 'acme-001',
                'acme_admin', 'open_remediation_ticket', 'device', '1LYSSFD074BB',
                '{"check_id": "screen_lock"}'::jsonb, 'rationale', '["snap_1"]'::jsonb,
                '2026-06-12T10:00:00Z', '{"decision": "allow"}'::jsonb, 'pending_approval',
                %s, 'hash_123', 'pol_1', %s
            )
            """,
            (prop_id, f"batch-{suffix}", tid, turn_id, f"ticket:1LYSSFD074BB:{suffix}", prop_id),
        )
    return prop_id


def test_frozen_columns_cannot_be_mutated(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    prop_id = _seed_pending(db_manager, "frozen")
    mutations = [
        ("arguments_json = '{\"tampered\": true}'::jsonb", ()),
        ("rationale = 'tampered'", ()),
        ("target_id = 'OTHER_DEV'", ()),
        ("tool_name = 'flag_device_for_replacement'", ()),
        ("dedupe_key = 'tampered'", ()),
        ("approval_hash = 'tampered'", ()),
        ("policy_version = 'tampered'", ()),
        ("idempotency_key = 'tampered'", ()),
        ("evidence_snapshot_ids = '[\"fake\"]'::jsonb", ()),
    ]
    for set_clause, _ in mutations:
        with pytest.raises(errors.InsufficientPrivilege, match="immutable"):
            with db_manager.copilot_cursor("acme-001") as cur:
                cur.execute(
                    f"UPDATE copilot.action_proposals SET {set_clause} WHERE proposal_id = %s",
                    (prop_id,),
                )


def test_illegal_and_legal_status_transitions(
    db_manager: DatabaseManager,
    reset_copilot_state: None,
) -> None:
    # Illegal: skip approval (pending -> executing, pending -> executed)
    p1 = _seed_pending(db_manager, "skip")
    for bad_status in ("executing", "executed", "stale_blocked", "failed"):
        with pytest.raises(errors.RaiseException):
            with db_manager.copilot_cursor("acme-001") as cur:
                cur.execute(
                    "UPDATE copilot.action_proposals SET status = %s WHERE proposal_id = %s",
                    (bad_status, p1),
                )

    # Legal path: pending -> approved -> executing -> executed
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "UPDATE copilot.action_proposals SET status = 'approved' WHERE proposal_id = %s",
            (p1,),
        )
    # Illegal from approved: revert to pending or skip executing -> executed
    for bad_status in ("pending_approval", "rejected", "expired", "executed"):
        with pytest.raises(errors.RaiseException):
            with db_manager.copilot_cursor("acme-001") as cur:
                cur.execute(
                    "UPDATE copilot.action_proposals SET status = %s WHERE proposal_id = %s",
                    (bad_status, p1),
                )
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "UPDATE copilot.action_proposals SET status = 'executing' WHERE proposal_id = %s",
            (p1,),
        )
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "UPDATE copilot.action_proposals SET status = 'executed' WHERE proposal_id = %s",
            (p1,),
        )
    # Terminal state cannot transition anywhere
    with pytest.raises(errors.RaiseException):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                "UPDATE copilot.action_proposals SET status = 'approved' WHERE proposal_id = %s",
                (p1,),
            )

    # Legal path 2: pending -> rejected (terminal)
    p2 = _seed_pending(db_manager, "rej")
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "UPDATE copilot.action_proposals SET status = 'rejected' WHERE proposal_id = %s",
            (p2,),
        )
    with pytest.raises(errors.RaiseException):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                "UPDATE copilot.action_proposals SET status = 'approved' WHERE proposal_id = %s",
                (p2,),
            )

    # Legal path 3: pending -> expired (terminal)
    p3 = _seed_pending(db_manager, "exp")
    with db_manager.copilot_cursor("acme-001") as cur:
        cur.execute(
            "UPDATE copilot.action_proposals SET status = 'expired' WHERE proposal_id = %s",
            (p3,),
        )
    with pytest.raises(errors.RaiseException):
        with db_manager.copilot_cursor("acme-001") as cur:
            cur.execute(
                "UPDATE copilot.action_proposals SET status = 'approved' WHERE proposal_id = %s",
                (p3,),
            )
