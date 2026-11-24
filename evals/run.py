"""Evaluation runner CLI (`python -m evals.run`) supporting replay, live, and `--max-cost`.

Note on per-repeat cassettes: each repeat uses a distinct `thread_id` (`..._r{rep}`),
which flows into deterministic `uuid5` `proposal_id`s in tool outputs. Therefore every repeat
must be recorded before replaying with `--repeats > 1` (or `--pass3-tags`).
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
import typer

from evals.metrics import (
    grade_actions,
    grade_citation_coverage,
    grade_citation_validity,
    grade_correctness,
    grade_insight_structure,
    grade_numeric_faithfulness,
    grade_safety,
    grade_trajectory,
    scan_audit_rows_for_privacy,
)
from evals.report import aggregate_results, cassette_set_hash, git_sha_short, write_report
from evals.schema import EvalCase, load_cases
from fleet_copilot.agent.llm import cassette_path_for
from fleet_copilot.agent.schemas import TurnResponse
from fleet_copilot.clock import FixedClock
from fleet_copilot.config import Settings, get_settings, test_database_overrides
from fleet_copilot.db.postgres import DatabaseManager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.runtime_config import get_runtime_config
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.copilot import CopilotService
from fleet_copilot.services.policy import PolicyService

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.telemetry_sim import reset_dataset, run_scenario  # noqa: E402

EVAL_FIXED_NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
ALL_TENANTS = ("acme-001", "globex-002", "initech-003")

app = typer.Typer(add_completion=False, help="Run Fleet Copilot evaluations (replay or live).")


def _configure_test_db_env(
    *,
    mode: Literal["replay", "live"],
    cassette_dir: Path | None = None,
) -> Settings:
    """Route eval execution strictly to TEST_DB_NAME so dev `fleet_copilot` is never mutated."""
    base = Settings()
    for env_k, env_v in test_database_overrides(base).items():
        os.environ[env_k] = env_v
    os.environ["LLM_REPLAY_MODE"] = "replay" if mode == "replay" else "record"
    if cassette_dir is not None:
        os.environ["LLM_CASSETTE_DIR"] = str(cassette_dir)
    if base.pseudonym_key is None and not os.environ.get("PSEUDONYM_KEY"):
        os.environ["PSEUDONYM_KEY"] = "0123456789abcdef" * 4
    if base.tenant_lock_key is None and not os.environ.get("TENANT_LOCK_KEY"):
        os.environ["TENANT_LOCK_KEY"] = "fedcba9876543210" * 4
    if base.langgraph_aes_key is None and not os.environ.get("LANGGRAPH_AES_KEY"):
        os.environ["LANGGRAPH_AES_KEY"] = "0123456789abcdef" * 2
    get_settings.cache_clear()
    return get_settings()


def _reset_copilot_state(admin_dsn: str) -> None:
    """Truncate copilot operational tables, audit chain heads, and checkpoints on test DB."""
    with psycopg.connect(admin_dsn, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE
                    copilot.charts,
                    copilot.turns,
                    copilot.threads,
                    copilot.action_proposals,
                    copilot.audit_logs,
                    copilot.audit_chain_heads,
                    copilot.checkpoints,
                    copilot.checkpoint_blobs,
                    copilot.checkpoint_writes
                RESTART IDENTITY CASCADE;
                """
            )
        conn.commit()


def _fetch_tenant_snapshot_ids(db: DatabaseManager, company_id: str) -> set[str]:
    with db.telemetry_ro_cursor(company_id) as cur:
        cur.execute("SELECT snapshot_id FROM telemetry.v_snapshots_deduped")
        return {str(r["snapshot_id"]) for r in cur.fetchall()}


def _fetch_proposal_statuses(db: DatabaseManager, company_id: str) -> tuple[dict[str, str], int]:
    with db.copilot_cursor(company_id) as cur:
        cur.execute("SELECT proposal_id, status FROM copilot.action_proposals")
        rows = cur.fetchall()
    statuses = {str(r["proposal_id"]): str(r["status"]) for r in rows}
    executed = sum(1 for st in statuses.values() if st == "executed")
    return statuses, executed


def _repeats_for_case(
    case: EvalCase,
    *,
    default_repeats: int,
    pass3_tags: set[str] | None,
) -> int:
    if pass3_tags and pass3_tags.intersection(case.tags):
        return max(default_repeats, 3)
    return default_repeats


def estimate_live_cost_usd(
    cases: list[EvalCase],
    *,
    model: str,
    repeats: int,
    pass3_tags: set[str] | None = None,
) -> float:
    """Estimate live Gemini spend using calibrated runtime.yaml pricing (~11k in / 900 out)."""
    rt = get_runtime_config()
    total_turns = sum(
        len(c.turns) * _repeats_for_case(c, default_repeats=repeats, pass3_tags=pass3_tags)
        for c in cases
    )
    est_in = total_turns * 11000
    est_out = total_turns * 900
    return round(rt.estimate_cost_usd(model, est_in, est_out), 4)


def run_eval_suite(
    *,
    mode: Literal["replay", "live"] = "replay",
    model: str | None = None,
    repeats: int = 1,
    pass3_tags: list[str] | None = None,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
    out_prefix: Path | None = None,
    cassette_dir: Path | None = None,
    thinking_override: Literal["minimal", "low", "medium", "high"] | None = None,
    skip_planner: bool = False,
    skip_verifier: bool = False,
    auto_confirm: bool = False,
    resume_existing: bool = False,
    max_cost: float | None = None,
) -> dict[str, Any]:
    """Execute the eval suite against the isolated test DB and write JSON + Markdown reports."""
    settings = _configure_test_db_env(mode=mode, cassette_dir=cassette_dir)
    resolved_model = model or settings.llm_model
    resolved_cassette_dir = Path(cassette_dir or settings.llm_cassette_dir)
    pass3_set = {t.strip() for t in (pass3_tags or []) if t.strip()} or None
    cases = load_cases(tags=tags, case_ids=case_ids)
    if not cases:
        raise ValueError("No evaluation cases matched the provided filters.")

    if mode == "live":
        est_cost = estimate_live_cost_usd(
            cases, model=resolved_model, repeats=repeats, pass3_tags=pass3_set
        )
        cap_note = f" (hard cap --max-cost=${max_cost:.2f})" if max_cost is not None else ""
        typer.echo(
            f"[live] {len(cases)} cases (repeats={repeats}, pass3_tags={sorted(pass3_set or [])}) "
            f"on {resolved_model} -> estimated cost ~${est_cost:.4f} USD{cap_note}"
        )
        if not auto_confirm:
            if not typer.confirm("Proceed with live Gemini API calls?", default=False):
                raise typer.Abort()

    reset_dataset(writer_url=settings.telemetry_writer_database_url)
    _reset_copilot_state(settings.admin_database_url)

    case_runs: list[dict[str, Any]] = []
    running_live_spend = 0.0
    stopped_by_cost_cap = False
    ablation_flags: dict[str, Any] = {}
    if thinking_override:
        ablation_flags["thinking_level"] = thinking_override
    if skip_planner:
        ablation_flags["skip_planner"] = True
    if skip_verifier:
        ablation_flags["skip_verifier"] = True

    with DatabaseManager(
        telemetry_ro_dsn=settings.telemetry_ro_database_url,
        copilot_dsn=settings.copilot_database_url,
        min_size=1,
        max_size=4,
    ) as db:
        audit_svc = AuditService(db)
        policy_svc = PolicyService(PgCopilotStore(db))
        baseline_snaps: dict[str, set[str]] = {
            cid: _fetch_tenant_snapshot_ids(db, cid) for cid in ALL_TENANTS
        }
        tenant_policies: dict[str, dict[str, Any]] = {
            cid: {
                k: v.value
                for k, v in policy_svc.resolve(
                    TenantContext(
                        company_id=cid, actor_id="eval_pol", role="it_admin", trace_id="eval_pol"
                    )
                ).items()
            }
            for cid in ALL_TENANTS
        }

        for c_idx, case in enumerate(cases, start=1):
            case_repeats = _repeats_for_case(case, default_repeats=repeats, pass3_tags=pass3_set)
            c_path = cassette_path_for(
                cassette_dir=resolved_cassette_dir,
                model_spec=resolved_model,
                case_or_session=case.id,
            )
            case_mode = mode
            if mode == "live":
                if resume_existing and c_path.exists() and c_path.stat().st_size > 0:
                    case_mode = "replay"
                else:
                    if max_cost is not None and running_live_spend >= max_cost:
                        typer.echo(
                            f"[STOP] Hit --max-cost=${max_cost:.2f} "
                            f"(running_spend=${running_live_spend:.4f}) before {case.id}. "
                            "Re-run with --resume-existing to continue."
                        )
                        stopped_by_cost_cap = True
                        break
                    if c_path.exists():
                        c_path.unlink()

            if case_mode == "replay":
                os.environ["LLM_REPLAY_MODE"] = "replay"
            else:
                os.environ["LLM_REPLAY_MODE"] = "record"
            get_settings.cache_clear()
            case_settings = get_settings()

            clock = FixedClock(EVAL_FIXED_NOW)
            svc = CopilotService(
                db=db,
                clock=clock,
                settings=case_settings,
                model_id=resolved_model,
                cassette_name=case.id,
                thinking_override=thinking_override,
                skip_planner=skip_planner,
                skip_verifier=skip_verifier,
            )

            case_start_idx = len(case_runs)
            for rep in range(1, case_repeats + 1):
                if case_mode == "live" and max_cost is not None and running_live_spend >= max_cost:
                    typer.echo(
                        f"[STOP] Hit --max-cost=${max_cost:.2f} "
                        f"(running_spend=${running_live_spend:.4f}) during {case.id} r{rep}."
                    )
                    stopped_by_cost_cap = True
                    break

                _reset_copilot_state(settings.admin_database_url)
                mutated_telemetry = False

                if case.setup_scenario:
                    run_scenario(
                        case.setup_scenario,
                        device_id=case.setup_device_id or "1LYSSFD074BB",
                        writer_url=settings.telemetry_writer_database_url,
                        company_id=case.tenant,
                    )
                    mutated_telemetry = True

                t0 = time.monotonic()
                thread_id = f"{case.tenant}:eval_{case.id}_r{rep}"
                all_turns: list[TurnResponse] = []

                try:
                    for t_idx, turn in enumerate(case.turns, start=1):
                        ctx = TenantContext(
                            company_id=case.tenant,
                            actor_id=f"eval_{case.tenant}_{case.role}",
                            role=case.role,
                            trace_id=f"eval-{case.id}-r{rep}-t{t_idx}",
                        )
                        turn_id = f"turn_{case.id}_r{rep}_t{t_idx}"
                        resp = svc.run_turn(ctx, thread_id, turn.user, turn_id=turn_id)
                        all_turns.append(resp)

                        if (
                            turn.between_pause_and_resume_scenario
                            and resp.status == "waiting_approval"
                        ):
                            run_scenario(
                                turn.between_pause_and_resume_scenario,
                                device_id=turn.between_scenario_device_id or "1LYSSFD074BB",
                                writer_url=settings.telemetry_writer_database_url,
                                company_id=case.tenant,
                            )
                            mutated_telemetry = True

                        if (
                            turn.decisions
                            and resp.status == "waiting_approval"
                            and resp.approval_packet
                        ):
                            packet_ids = [
                                str(p["proposal_id"])
                                for p in resp.approval_packet.get("proposals", [])
                            ]
                            resolved_decisions: dict[str, str] = {}
                            for pid in packet_ids:
                                if pid in turn.decisions:
                                    resolved_decisions[pid] = turn.decisions[pid]
                                elif "*" in turn.decisions:
                                    resolved_decisions[pid] = turn.decisions["*"]
                            if resolved_decisions:
                                resume_resp = svc.resume(
                                    ctx,
                                    thread_id,
                                    resolved_decisions,
                                    note="Eval runner decision",
                                )
                                all_turns.append(resume_resp)

                    latency_ms = int((time.monotonic() - t0) * 1000)
                    final_resp = all_turns[-1]

                    # Persist each case's TurnResponse JSON for the Examples gallery (rep 1)
                    if rep == 1 and out_prefix is not None:
                        runs_dir = out_prefix.parent / "runs"
                        runs_dir.mkdir(parents=True, exist_ok=True)
                        (runs_dir / f"{case.id}.json").write_text(
                            final_resp.model_dump_json(indent=2),
                            encoding="utf-8",
                        )

                    valid_snaps = (
                        _fetch_tenant_snapshot_ids(db, case.tenant)
                        if mutated_telemetry
                        else baseline_snaps[case.tenant]
                    )
                    prop_statuses, executed_count = _fetch_proposal_statuses(db, case.tenant)

                    # ★ Audit chain verify, privacy scan, & grounding_violation telemetry
                    audit_ok = True
                    all_audit_rows: list[dict[str, Any]] = []
                    for cid in ALL_TENANTS:
                        vctx = TenantContext(
                            company_id=cid,
                            actor_id="eval_auditor",
                            role="it_admin",
                            trace_id=f"eval-verify-{case.id}-r{rep}",
                        )
                        vres = audit_svc.verify(vctx)
                        if not vres.get("ok"):
                            audit_ok = False
                        all_audit_rows.extend(audit_svc.list(vctx, limit=500))

                    privacy_violations = scan_audit_rows_for_privacy(all_audit_rows)
                    privacy_ok = len(privacy_violations) == 0

                    gv_events = [
                        r for r in all_audit_rows if r.get("event_type") == "grounding_violation"
                    ]
                    first_pass_grounded = len(gv_events) == 0
                    verifier_retries = sum(
                        1
                        for r in gv_events
                        if (r.get("payload_json") or {}).get("outcome") == "retried"
                    )
                    claims_stripped = sum(
                        int(((r.get("payload_json") or {}).get("counts") or {}).get("hard", 0))
                        for r in gv_events
                        if (r.get("payload_json") or {}).get("outcome") == "stripped"
                    )

                    correctness = grade_correctness(case, final_resp)
                    cit_valid = grade_citation_validity(
                        final_resp,
                        valid_snapshot_ids_for_tenant=valid_snaps,
                        require_citations=(case.expect.citations == "required"),
                    )
                    for earlier in all_turns[:-1]:
                        earlier_cit = grade_citation_validity(
                            earlier,
                            valid_snapshot_ids_for_tenant=valid_snaps,
                            require_citations=False,
                        )
                        if not earlier_cit["passed"]:
                            cit_valid["passed"] = False
                            cit_valid["score"] = 0.0
                            cit_valid["invalid_reasons"].extend(earlier_cit["invalid_reasons"])

                    cit_cov = grade_citation_coverage(final_resp)
                    ins_struct = grade_insight_structure(final_resp)
                    num_faith = grade_numeric_faithfulness(
                        final_resp,
                        resolved_policies=tenant_policies[case.tenant],
                        user_message=case.turns[-1].user,
                    )
                    traj = grade_trajectory(case, all_turns)
                    actions = grade_actions(case, all_turns, db_proposal_statuses=prop_statuses)
                    safety = grade_safety(case, all_turns, executed_count_in_db=executed_count)

                    seen_turn_ids: set[str] = set()
                    tokens_in = 0
                    tokens_out = 0
                    cost_est = 0.0
                    for r in all_turns:
                        if r.turn_id not in seen_turn_ids:
                            seen_turn_ids.add(r.turn_id)
                            tokens_in += r.usage.input_tokens
                            tokens_out += r.usage.output_tokens
                            cost_est += r.usage.cost_usd_est

                    if case_mode == "live":
                        running_live_spend += cost_est

                    case_passed = (
                        bool(correctness["passed"])
                        and bool(cit_valid["passed"])
                        and bool(traj["passed"])
                        and bool(actions["passed"])
                        and bool(safety["passed"])
                        and audit_ok
                        and privacy_ok
                    )

                    case_runs.append(
                        {
                            "case_id": case.id,
                            "repeat": rep,
                            "tags": list(case.tags),
                            "has_action_expectation": case.expect.proposals is not None,
                            "status": final_resp.status,
                            "case_passed": case_passed,
                            "correctness": correctness,
                            "citation_validity": cit_valid,
                            "citation_coverage": cit_cov,
                            "insight_structure": ins_struct,
                            "numeric_faithfulness": num_faith,
                            "first_pass_grounded": first_pass_grounded,
                            "verifier_retries": verifier_retries,
                            "claims_stripped": claims_stripped,
                            "trajectory": traj,
                            "actions": actions,
                            "safety": safety,
                            "audit_verify_ok": audit_ok,
                            "privacy_scan_ok": privacy_ok,
                            "privacy_violations": privacy_violations,
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "cost_usd_est": round(cost_est, 6),
                            "latency_ms": latency_ms,
                        }
                    )
                finally:
                    if mutated_telemetry:
                        reset_dataset(writer_url=settings.telemetry_writer_database_url)

            slice_runs = case_runs[case_start_idx:]
            passed_reps = sum(1 for r in slice_runs if r["case_passed"])
            c_cost = sum(float(r["cost_usd_est"]) for r in slice_runs)
            tag_mode = "reused" if case_mode != mode else mode
            cost_label = "recorded_cost" if case_mode == "replay" else "cost"
            spend_str = f" running_live=${running_live_spend:.4f}" if mode == "live" else ""
            typer.echo(
                f"[{c_idx}/{len(cases)}] {case.id} ({tag_mode}): "
                f"pass={passed_reps}/{len(slice_runs)} {cost_label}=${c_cost:.5f}{spend_str}"
            )
            if stopped_by_cost_cap:
                break

    _reset_copilot_state(settings.admin_database_url)

    report = aggregate_results(
        case_runs,
        model=resolved_model,
        mode=mode,
        repeats=repeats,
        cassette_hash=cassette_set_hash(resolved_cassette_dir, resolved_model),
        git_sha=git_sha_short(REPO_ROOT),
        ablation_flags=ablation_flags,
    )
    report["metadata"]["stopped_by_cost_cap"] = stopped_by_cost_cap
    report["metadata"]["running_live_spend_usd"] = round(running_live_spend, 6)
    if out_prefix is not None:
        write_report(report, out_prefix)
    return report


DEFAULT_BASELINE_PREFIX = Path("evals/reports/latest")
DEFAULT_REPLAY_PREFIX = Path("evals/reports/replay/latest")


def resolve_eval_out_prefix(
    *,
    mode: Literal["replay", "live"],
    out: Path | None,
    write_baseline: bool,
) -> Path:
    """Resolve report output prefix: replay writes outside the baseline unless --write-baseline."""
    if out is not None:
        return out
    if write_baseline or mode == "live":
        return DEFAULT_BASELINE_PREFIX
    return DEFAULT_REPLAY_PREFIX


def compute_eval_exit_code(
    report: dict[str, Any],
    *,
    baseline_path: Path | None = None,
) -> tuple[int, list[str]]:
    """Compute CLI exit code: 2 on cost cap, 1 on hard gate failure or baseline regression, 0 ok."""
    if report["metadata"].get("stopped_by_cost_cap"):
        return 2, []

    gates = report["gates"]["gates"]
    hard_ok = all(bool(g["passed"]) for g in gates if g.get("hard", True))
    regressions: list[str] = []

    if baseline_path is not None and baseline_path.exists():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            baseline = None
        if isinstance(baseline, dict) and isinstance(baseline.get("cases"), list):
            base_by_id = {
                str(c["case_id"]): bool(c["pass_k"])
                for c in baseline["cases"]
                if isinstance(c, dict) and "case_id" in c and "pass_k" in c
            }
            for cur in report.get("cases", []):
                cid = str(cur["case_id"])
                if cid in base_by_id and bool(cur["pass_k"]) != base_by_id[cid]:
                    regressions.append(
                        f"{cid}: baseline pass_k={base_by_id[cid]} != current {bool(cur['pass_k'])}"
                    )

    if not hard_ok or regressions:
        return 1, regressions
    return 0, []


@app.command()
def main(
    mode: Annotated[
        Literal["replay", "live"],
        typer.Option("--mode", help="Execution mode: replay (offline) | live (record)."),
    ] = "replay",
    model: Annotated[
        str | None,
        typer.Option("--model", help="Gemini model spec (default from Settings.llm_model)."),
    ] = None,
    repeats: Annotated[
        int,
        typer.Option("--repeats", min=1, max=10, help="Default repeats per case."),
    ] = 1,
    pass3_tags: Annotated[
        str | None,
        typer.Option(
            "--pass3-tags",
            help="Comma-separated tags to run 3 repeats on (e.g. actions,redteam).",
        ),
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option("--tags", help="Comma-separated tag filter (e.g. qa,redteam)."),
    ] = None,
    cases: Annotated[
        str | None,
        typer.Option("--cases", help="Comma-separated case ID filter."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Output path prefix (default: evals/reports/replay/latest in replay; "
            "evals/reports/latest with --write-baseline or live).",
        ),
    ] = None,
    write_baseline: Annotated[
        bool,
        typer.Option(
            "--write-baseline",
            help="Write evals/reports/latest.{json,md} and runs/*.json as the new baseline.",
        ),
    ] = False,
    thinking: Annotated[
        Literal["minimal", "low", "medium", "high"] | None,
        typer.Option("--thinking", help="Optional thinking_level override for ablations."),
    ] = None,
    skip_planner: Annotated[
        bool,
        typer.Option("--skip-planner", help="Ablation: bypass planner node."),
    ] = False,
    skip_verifier: Annotated[
        bool,
        typer.Option("--skip-verifier", help="Ablation: bypass grounding verifier node."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip interactive confirmation in live mode."),
    ] = False,
    resume_existing: Annotated[
        bool,
        typer.Option(
            "--resume-existing",
            help="In live mode, replay cases whose cassette already exists.",
        ),
    ] = False,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            min=0.01,
            help="Hard USD spend cap for live runs; stops cleanly before exceeding.",
        ),
    ] = None,
) -> None:
    """Run the Fleet Copilot evaluation suite and exit non-zero on hard gate or regression fail."""
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    case_list = [c.strip() for c in cases.split(",")] if cases else None
    pass3_list = [t.strip() for t in pass3_tags.split(",")] if pass3_tags else None
    resolved_out = resolve_eval_out_prefix(mode=mode, out=out, write_baseline=write_baseline)
    baseline_json = DEFAULT_BASELINE_PREFIX.with_suffix(".json")
    check_baseline = (not write_baseline) and (resolved_out != DEFAULT_BASELINE_PREFIX)

    report = run_eval_suite(
        mode=mode,
        model=model,
        repeats=repeats,
        pass3_tags=pass3_list,
        tags=tag_list,
        case_ids=case_list,
        out_prefix=resolved_out,
        thinking_override=thinking,
        skip_planner=skip_planner,
        skip_verifier=skip_verifier,
        auto_confirm=yes,
        resume_existing=resume_existing,
        max_cost=max_cost,
    )

    s = report["summary"]
    gates = report["gates"]
    sum_cost_label = "recorded_cost" if mode == "replay" else "cost"
    typer.echo(
        f"Eval complete ({mode}, {report['metadata']['model']}): "
        f"cases={s['total_cases']} runs={s['total_runs']} "
        f"overall={s['overall_pass_rate']:.4f} pass_k={s['pass_k_rate']:.4f} "
        f"first_pass_grounding={s['first_pass_grounding_rate']:.4f} "
        f"{sum_cost_label}=${s['total_cost_usd_est']:.4f}"
    )
    for g in gates["gates"]:
        mark = str(
            g.get("status")
            or ("PASS" if g["passed"] else ("FAIL" if g.get("hard", True) else "BELOW TARGET"))
        )
        typer.echo(f"  [{mark}] {g['name']}: {g['actual']:.4f} (target >= {g['threshold']:.2f})")
    typer.echo(
        f"Report written to {resolved_out.with_suffix('.md')} and "
        f"{resolved_out.with_suffix('.json')}"
    )

    exit_code, regressions = compute_eval_exit_code(
        report,
        baseline_path=baseline_json if check_baseline else None,
    )
    if regressions:
        typer.echo(f"Baseline regressions vs {baseline_json}:")
        for reg in regressions:
            typer.echo(f"  [DIFF] {reg}")
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


if __name__ == "__main__":
    app()
