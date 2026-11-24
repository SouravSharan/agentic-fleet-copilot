"""Aggregation and Markdown + JSON report writer for evaluation runs (07 Step 6 Task 6.5)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.metrics import evaluate_ci_gates
from fleet_copilot.agent.llm import parse_model_name


def git_sha_short(repo_root: Path) -> str:
    """Return short HEAD git SHA or `'unknown'` if outside git."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def cassette_set_hash(cassette_dir: Path, model_spec: str) -> str:
    """Compute a deterministic SHA-256 hash over all cassette JSONL files for `model_spec`."""
    bare = parse_model_name(model_spec).replace("/", "_")
    model_dir = cassette_dir / bare
    if not model_dir.exists():
        return "none"
    h = hashlib.sha256()
    for path in sorted(model_dir.glob("*.jsonl")):
        h.update(path.name.encode("utf-8"))
        h.update(b"\x00")
        h.update(path.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _mean(vals: list[float]) -> float:
    return round(sum(vals) / len(vals), 4) if vals else 1.0


def aggregate_results(
    case_runs: list[dict[str, Any]],
    *,
    model: str,
    mode: str,
    repeats: int,
    cassette_hash: str,
    git_sha: str,
    ablation_flags: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate per-case repeat runs into summary metrics, pass^k stability, and CI gates."""
    by_case: dict[str, list[dict[str, Any]]] = {}
    for run in case_runs:
        by_case.setdefault(str(run["case_id"]), []).append(run)

    per_case_summary: list[dict[str, Any]] = []
    pass_k_flags: list[float] = []

    for cid in sorted(by_case):
        runs = by_case[cid]
        all_passed = all(bool(r["case_passed"]) for r in runs)
        pass_k_flags.append(1.0 if all_passed else 0.0)
        per_case_summary.append(
            {
                "case_id": cid,
                "tags": runs[0]["tags"],
                "repeats": len(runs),
                "passed_repeats": sum(1 for r in runs if r["case_passed"]),
                "pass_k": all_passed,
                "correctness": _mean([float(r["correctness"]["score"]) for r in runs]),
                "citation_validity": _mean([float(r["citation_validity"]["score"]) for r in runs]),
                "citation_coverage": _mean([float(r["citation_coverage"]) for r in runs]),
                "numeric_faithfulness": _mean([float(r["numeric_faithfulness"]) for r in runs]),
                "trajectory_pass": all(bool(r["trajectory"]["passed"]) for r in runs),
                "action_score": _mean([float(r["actions"]["score"]) for r in runs]),
                "safety_pass": all(bool(r["safety"]["passed"]) for r in runs),
                "audit_verify_pass": all(bool(r["audit_verify_ok"]) for r in runs),
                "privacy_scan_pass": all(bool(r["privacy_scan_ok"]) for r in runs),
                "tokens_in": sum(int(r["tokens_in"]) for r in runs),
                "tokens_out": sum(int(r["tokens_out"]) for r in runs),
                "cost_usd_est": round(sum(float(r["cost_usd_est"]) for r in runs), 6),
                "mean_latency_ms": round(sum(int(r["latency_ms"]) for r in runs) / len(runs), 1),
            }
        )

    qa_runs = [r for r in case_runs if "qa" in r["tags"] and "redteam" not in r["tags"]]
    ins_runs = [r for r in case_runs if "insights" in r["tags"]]
    act_runs = [
        r
        for r in case_runs
        if ("actions" in r["tags"] or r.get("has_action_expectation"))
        and "redteam" not in r["tags"]
    ]
    rt_runs = [r for r in case_runs if "redteam" in r["tags"]]
    mt_runs = [r for r in case_runs if "multiturn" in r["tags"]]

    ins_recalls = [
        float(r["correctness"]["recall"])
        for r in ins_runs
        if r["correctness"].get("recall") is not None
    ]
    ins_precisions = [
        float(r["correctness"]["precision"])
        for r in ins_runs
        if r["correctness"].get("precision") is not None
    ]

    summary: dict[str, Any] = {
        "total_cases": len(by_case),
        "total_runs": len(case_runs),
        "repeats": repeats,
        "overall_pass_rate": _mean([1.0 if r["case_passed"] else 0.0 for r in case_runs]),
        "pass_k_rate": _mean(pass_k_flags),
        "citation_validity": _mean([float(r["citation_validity"]["score"]) for r in case_runs]),
        "citation_coverage": _mean([float(r["citation_coverage"]) for r in case_runs]),
        "numeric_faithfulness": _mean([float(r["numeric_faithfulness"]) for r in case_runs]),
        "first_pass_grounding_rate": _mean(
            [1.0 if r.get("first_pass_grounded", True) else 0.0 for r in case_runs]
        ),
        "verifier_retries_total": sum(int(r.get("verifier_retries", 0) or 0) for r in case_runs),
        "claims_stripped_total": sum(int(r.get("claims_stripped", 0) or 0) for r in case_runs),
        "trajectory_pass_rate": _mean(
            [1.0 if r["trajectory"]["passed"] else 0.0 for r in case_runs]
        ),
        "safety_rate": _mean([1.0 if r["safety"]["passed"] else 0.0 for r in case_runs]),
        "redteam_pass_rate": _mean([1.0 if r["safety"]["passed"] else 0.0 for r in rt_runs]),
        "audit_verify_rate": _mean([1.0 if r["audit_verify_ok"] else 0.0 for r in case_runs]),
        "privacy_scan_rate": _mean([1.0 if r["privacy_scan_ok"] else 0.0 for r in case_runs]),
        "qa_count": len(qa_runs),
        "qa_correctness": _mean([float(r["correctness"]["score"]) for r in qa_runs]),
        "insights_count": len(ins_runs),
        "insights_recall": _mean(ins_recalls),
        "insights_precision": _mean(ins_precisions),
        "actions_count": len(act_runs),
        "action_accuracy": _mean([float(r["actions"]["score"]) for r in act_runs]),
        "multiturn_count": len(mt_runs),
        "multiturn_pass_rate": _mean([1.0 if r["case_passed"] else 0.0 for r in mt_runs]),
        "redteam_count": len(rt_runs),
        "total_input_tokens": sum(int(r["tokens_in"]) for r in case_runs),
        "total_output_tokens": sum(int(r["tokens_out"]) for r in case_runs),
        "total_cost_usd_est": round(sum(float(r["cost_usd_est"]) for r in case_runs), 6),
        "mean_latency_ms": round(
            (sum(int(r["latency_ms"]) for r in case_runs) / len(case_runs)) if case_runs else 0.0,
            1,
        ),
    }
    gates = evaluate_ci_gates(summary)

    return {
        "metadata": {
            "generated_at": datetime.now(UTC).isoformat(),
            "git_sha": git_sha,
            "model": model,
            "mode": mode,
            "repeats": repeats,
            "cassette_set_hash": cassette_hash,
            "ablations": ablation_flags or {},
        },
        "summary": summary,
        "gates": gates,
        "cases": per_case_summary,
        "runs": case_runs,
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    """Format an aggregated evaluation report as concise GitHub-flavored Markdown."""
    meta = report["metadata"]
    s = report["summary"]
    gates = report["gates"]
    hard_passed = bool(gates.get("hard_passed", gates["passed"]))
    status_badge = "PASS" if hard_passed else "FAIL"

    lines: list[str] = [
        f"# Fleet Copilot Evaluation Report ({status_badge})",
        "",
        f"- **Date (UTC):** `{meta['generated_at']}`",
        f"- **Model:** `{meta['model']}`",
        f"- **Mode:** `{meta['mode']}` · **Repeats:** `{meta['repeats']}`",
        f"- **Git SHA:** `{meta['git_sha']}` · **Cassette hash:** `{meta['cassette_set_hash']}`",
    ]
    if meta.get("ablations"):
        lines.append(f"- **Ablations:** `{json.dumps(meta['ablations'], sort_keys=True)}`")

    lines.extend(
        [
            "",
            "## CI Gates",
            "",
            "| Gate | Actual | Target | Status |",
            "|---|---:|---:|:---:|",
        ]
    )
    for g in gates["gates"]:
        mark = str(
            g.get("status")
            or ("PASS" if g["passed"] else ("FAIL" if g.get("hard", True) else "BELOW TARGET"))
        )
        lines.append(f"| {g['name']} | {g['actual']:.4f} | >= {g['threshold']:.2f} | {mark} |")

    cost_row_label = (
        "Recorded LLM cost (`recorded_cost`, USD)"
        if meta.get("mode") == "replay"
        else "Estimated LLM cost (USD)"
    )
    lines.extend(
        [
            "",
            "## Summary Metrics",
            "",
            "| Metric | Value |",
            "|---|---:|",
            f"| Cases / Total runs | {s['total_cases']} / {s['total_runs']} |",
            f"| Overall pass rate | {s['overall_pass_rate']:.4f} |",
            f"| Stability (`pass^{s['repeats']}`) | {s['pass_k_rate']:.4f} |",
            f"| QA correctness ({s['qa_count']} runs) | {s['qa_correctness']:.4f} |",
            (
                f"| Insights recall / precision ({s['insights_count']} runs) "
                f"| {s['insights_recall']:.4f} / {s['insights_precision']:.4f} |"
            ),
            f"| Action accuracy ({s['actions_count']} runs) | {s['action_accuracy']:.4f} |",
            (
                f"| Multi-turn pass rate ({s['multiturn_count']} runs) "
                f"| {s['multiturn_pass_rate']:.4f} |"
            ),
            f"| Red-team pass rate ({s['redteam_count']} runs) | {s['redteam_pass_rate']:.4f} |",
            f"| Citation coverage | {s['citation_coverage']:.4f} |",
            f"| Numeric faithfulness (independent) | {s['numeric_faithfulness']:.4f} |",
            f"| First-pass grounding rate | {s['first_pass_grounding_rate']:.4f} |",
            (
                f"| Verifier retries / claims stripped "
                f"| {s['verifier_retries_total']} / {s['claims_stripped_total']} |"
            ),
            f"| Trajectory pass rate | {s['trajectory_pass_rate']:.4f} |",
            (
                f"| Total tokens (in / out) "
                f"| {s['total_input_tokens']:,} / {s['total_output_tokens']:,} |"
            ),
            f"| {cost_row_label} | ${s['total_cost_usd_est']:.4f} |",
            f"| Mean latency (ms) | {s['mean_latency_ms']:.1f} |",
        ]
    )

    failing_cases = [c for c in report["cases"] if not c["pass_k"]]
    if failing_cases:
        lines.extend(
            [
                "",
                "## Failing or Unstable Cases",
                "",
                "| Case ID | Passed | Correctness | CitValid | Safety | Actions |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for c in failing_cases:
            lines.append(
                f"| `{c['case_id']}` | {c['passed_repeats']}/{c['repeats']} | "
                f"{c['correctness']:.2f} | {c['citation_validity']:.2f} | "
                f"{'OK' if c['safety_pass'] else 'FAIL'} | {c['action_score']:.2f} |"
            )
    else:
        lines.extend(["", "## Failures", "", "None — all cases passed every repeat."])

    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_prefix: Path) -> tuple[Path, Path]:
    """Write `<out_prefix>.json` and `<out_prefix>.md`, creating parent directories as needed."""
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_prefix.with_suffix(".json")
    md_path = out_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return json_path, md_path
