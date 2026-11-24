"""Typer + Rich CLI (`fleet-copilot`) for chat, ask, proposals, decide, and audit."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from fleet_copilot.clock import SystemClock
from fleet_copilot.config import get_settings
from fleet_copilot.db.postgres import get_db_manager
from fleet_copilot.domain.models import TenantContext
from fleet_copilot.observability.logging import configure_logging
from fleet_copilot.repositories.copilot_store import PgCopilotStore
from fleet_copilot.repositories.telemetry import PgTelemetryRepository
from fleet_copilot.services.actions import ActionService
from fleet_copilot.services.adapters.mock import MockAdapter
from fleet_copilot.services.audit import AuditService
from fleet_copilot.services.briefing import build_briefing
from fleet_copilot.services.copilot import CopilotService
from fleet_copilot.services.demo_auth import load_personas
from fleet_copilot.services.guarded import GuardedCopilot
from fleet_copilot.services.policy import PolicyService

app = typer.Typer(add_completion=False, help="Fleet Copilot CLI (chat, ask, proposals, audit).")
console = Console()


def _ctx(
    company: str, role: Literal["viewer", "it_admin"], actor: str | None = None
) -> TenantContext:
    return TenantContext(
        company_id=company,
        actor_id=actor or f"{company}_{role}",
        role=role,
        trace_id=f"cli-{uuid.uuid4().hex[:12]}",
    )


def _render_trace(resp: Any) -> None:
    if resp.plan is not None:
        steps = ", ".join(s.tool_hint or s.goal for s in resp.plan.steps) or "none"
        console.print(f"[dim]Plan ({resp.plan.intent}): {steps}[/dim]")
    if resp.tool_trace:
        parts = [f"{t['tool_name']}({t['duration_ms']}ms)" for t in resp.tool_trace]
        console.print(f"[dim]Tools: {' -> '.join(parts)}[/dim]")


def _build_copilot(cassette: str | None = None) -> GuardedCopilot:
    settings = get_settings()
    configure_logging(log_format=settings.log_format, log_level=settings.log_level)
    db = get_db_manager()
    copilot = CopilotService(db=db, settings=settings, cassette_name=cassette)
    return GuardedCopilot(copilot, settings=settings)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="Question or instruction for Fleet Copilot.")],
    company: Annotated[str, typer.Option(help="Tenant company_id.")] = "acme-001",
    role: Annotated[str, typer.Option(help="Actor role: viewer | it_admin.")] = "it_admin",
    thread_id: Annotated[str | None, typer.Option(help="Optional existing thread_id.")] = None,
    cassette: Annotated[str | None, typer.Option(help="Optional replay cassette name.")] = None,
) -> None:
    """Run a single turn and print the structured `TurnResponse` JSON."""
    if role not in ("viewer", "it_admin"):
        raise typer.BadParameter("role must be 'viewer' or 'it_admin'")
    svc = _build_copilot(cassette=cassette)
    resp = svc.run_turn(_ctx(company, role), thread_id, question)  # type: ignore[arg-type]
    typer.echo(resp.model_dump_json(indent=2))


@app.command()
def chat(
    company: Annotated[str, typer.Option(help="Tenant company_id.")] = "acme-001",
    role: Annotated[str, typer.Option(help="Actor role: viewer | it_admin.")] = "it_admin",
    thread_id: Annotated[
        str | None,
        typer.Option("--thread-id", help="Resume an existing conversation thread_id."),
    ] = None,
    cassette: Annotated[str | None, typer.Option(help="Optional replay cassette name.")] = None,
) -> None:
    """Interactive REPL with dimmed plan/tool trace, footnotes, and a/r/d approval prompts."""
    if role not in ("viewer", "it_admin"):
        raise typer.BadParameter("role must be 'viewer' or 'it_admin'")
    svc = _build_copilot(cassette=cassette)
    active_tid = thread_id or f"{company}:{uuid.uuid4().hex[:12]}"
    if thread_id:
        prior = svc.get_thread(_ctx(company, role), active_tid)  # type: ignore[arg-type]
        if prior and prior.get("turns"):
            console.print(
                f"[dim]Resuming thread {active_tid} ({len(prior['turns'])} prior turn(s))[/dim]"
            )
    console.print(
        Panel.fit(
            f"Fleet Copilot · tenant=[bold]{company}[/bold] · role=[bold]{role}[/bold]\n"
            f"thread=[dim]{active_tid}[/dim] · type 'exit' to quit"
        )
    )

    while True:
        try:
            user_input = console.input("[bold cyan]you>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input or user_input.lower() in ("exit", "quit"):
            break

        ctx = _ctx(company, role)  # type: ignore[arg-type]
        resp = svc.run_turn(ctx, active_tid, user_input)
        _render_trace(resp)
        console.print(Markdown(resp.answer_markdown))
        console.print(
            f"[dim]as_of={resp.telemetry_as_of} · tokens={resp.usage.total_tokens} · "
            f"est=${resp.usage.cost_usd_est:.4f} · trace={resp.trace_id}[/dim]"
        )

        if resp.status == "waiting_approval" and resp.approval_packet:
            decisions: dict[str, str] = {}
            for prop in resp.approval_packet.get("proposals", []):
                console.print(
                    Panel(
                        f"Tool: [bold]{prop['tool_name']}[/bold]\n"
                        f"Target: {prop['target_type']} `{prop['target_id']}`\n"
                        f"Args: {prop['arguments']}\n"
                        f"Rationale: {prop['rationale']}",
                        title=f"Approval required: {prop['proposal_id']}",
                    )
                )
                choice = (
                    console.input("[bold yellow][a]pprove / [r]eject / [d]efer>[/bold yellow] ")
                    .strip()
                    .lower()
                )
                decisions[prop["proposal_id"]] = (
                    "approve"
                    if choice.startswith("a")
                    else ("reject" if choice.startswith("r") else "defer")
                )
            resumed = svc.resume(_ctx(company, role), active_tid, decisions)  # type: ignore[arg-type]
            console.print(Markdown(resumed.answer_markdown))


@app.command()
def threads(
    company: Annotated[str, typer.Option(help="Tenant company_id.")] = "acme-001",
    role: Annotated[str, typer.Option(help="Actor role: viewer | it_admin.")] = "it_admin",
    search: Annotated[str | None, typer.Option(help="Filter threads by title substring.")] = None,
    limit: Annotated[int, typer.Option(help="Max threads to list.")] = 20,
) -> None:
    """List saved conversation threads owned by the current actor."""
    if role not in ("viewer", "it_admin"):
        raise typer.BadParameter("role must be 'viewer' or 'it_admin'")
    svc = _build_copilot()
    rows = svc.list_threads(_ctx(company, role), search=search, limit=limit)  # type: ignore[arg-type]
    table = Table(title=f"Saved Threads ({company}, {role})")
    for col in ("thread_id", "title", "status", "updated_at"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["thread_id"]),
            str(r.get("title") or "-"),
            str(r.get("status") or "active"),
            str(r.get("updated_at") or "-"),
        )
    console.print(table)


@app.command()
def proposals(
    company: Annotated[str, typer.Option(help="Tenant company_id.")] = "acme-001",
    status: Annotated[str | None, typer.Option(help="Optional status filter.")] = None,
    device_id: Annotated[str | None, typer.Option(help="Optional device_id filter.")] = None,
) -> None:
    """List action proposals for `company`."""
    db = get_db_manager()
    store = PgCopilotStore(db)
    svc = ActionService(
        db, PgTelemetryRepository(db), store, PolicyService(store), AuditService(db), MockAdapter()
    )
    rows = svc.list(_ctx(company, "it_admin"), status=status, device_id=device_id)
    table = Table(title=f"Action Proposals ({company})")
    for col in ("proposal_id", "tool_name", "target_id", "status", "reviewer_id"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["proposal_id"]),
            str(r["tool_name"]),
            str(r["target_id"]),
            str(r["status"]),
            str(r.get("reviewer_id") or "-"),
        )
    console.print(table)


@app.command()
def decide(
    proposal_id: Annotated[str, typer.Argument(help="Proposal ID to decide.")],
    decision: Annotated[str, typer.Argument(help="approve | reject | defer")],
    company: Annotated[str, typer.Option(help="Tenant company_id.")] = "acme-001",
    note: Annotated[str | None, typer.Option(help="Optional review note.")] = None,
) -> None:
    """Decide a pending proposal directly in the queue (and execute if approved)."""
    if decision not in ("approve", "reject", "defer"):
        raise typer.BadParameter("decision must be approve, reject, or defer")
    db = get_db_manager()
    store = PgCopilotStore(db)
    svc = ActionService(
        db, PgTelemetryRepository(db), store, PolicyService(store), AuditService(db), MockAdapter()
    )
    ctx = _ctx(company, "it_admin")
    clock = SystemClock()
    res = svc.decide(ctx, proposal_id, decision, "queue", clock, note=note)  # type: ignore[arg-type]
    typer.echo(f"Decision: applied={res.applied} message={res.message}")
    if res.applied and decision == "approve":
        exe = svc.execute(ctx, proposal_id, clock)
        typer.echo(f"Execute: status={exe.status} message={exe.message}")


@app.command()
def audit(
    company: Annotated[str, typer.Option(help="Tenant company_id.")] = "acme-001",
    trace: Annotated[str | None, typer.Option("--trace", help="Filter by trace_id.")] = None,
    verify: Annotated[bool, typer.Option("--verify", help="Verify hash chain integrity.")] = False,
) -> None:
    """Display tenant audit events and optionally verify the tamper-evident hash chain."""
    db = get_db_manager()
    audit_svc = AuditService(db)
    ctx = _ctx(company, "it_admin")
    if verify:
        res = audit_svc.verify(ctx)
        typer.echo(
            f"Audit chain ({company}): ok={res['ok']} "
            f"rows_checked={res['rows_checked']} first_bad_seq={res['first_bad_seq']}"
        )
        if not res["ok"]:
            raise typer.Exit(code=1)
    rows = audit_svc.list(ctx, trace_id=trace, limit=50)
    table = Table(title=f"Audit Log ({company})")
    for col in ("seq_no", "event_type", "actor_id", "trace_id", "payload_json"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r["seq_no"]),
            str(r["event_type"]),
            str(r["actor_id"]),
            str(r["trace_id"]),
            str(r["payload_json"]),
        )
    console.print(table)


@app.command()
def brief(
    persona: Annotated[
        str,
        typer.Option(
            "--persona",
            help="Persona from config/personas.yaml (e.g. acme_admin, globex_admin).",
        ),
    ] = "acme_admin",
) -> None:
    """Print the deterministic Fleet Briefing for a demo persona ($0, no LLM)."""
    personas = load_personas()
    spec = personas.get(persona)
    if spec is None:
        valid = ", ".join(sorted(personas.keys()))
        raise typer.BadParameter(f"Unknown persona {persona!r}. Choose from: {valid}")

    db = get_db_manager()
    repo = PgTelemetryRepository(db)
    store = PgCopilotStore(db)
    policy_svc = PolicyService(store)
    ctx = TenantContext(
        company_id=spec.company_id,
        actor_id=f"cli:{spec.id}",
        role=spec.role,
        trace_id=f"cli-brief-{uuid.uuid4().hex[:12]}",
    )
    briefing = build_briefing(ctx, repo, store, policy_svc)

    k = briefing.kpis
    console.print(
        Panel.fit(
            f"[bold]{briefing.company_name} Fleet Briefing[/bold] · persona={spec.id} "
            f"({spec.role}) · as_of={briefing.as_of[:10]}\n"
            f"Devices: [bold]{k.devices}[/bold] ({k.laptops} laptops) · "
            f"Compliance pass: [bold]{k.compliance_pass_pct:.1f}%[/bold] · "
            f"Open approvals: [bold]{k.open_approvals}[/bold] · "
            f"Needing attention: [bold]{k.devices_needing_attention}[/bold]"
        )
    )
    if not briefing.items:
        console.print("No active fleet findings.")
        return

    table = Table(title="Prioritized Findings")
    for col in ("Severity", "Category", "Title", "Devices", "Suggested prompt"):
        table.add_column(col)
    for item in briefing.items:
        table.add_row(
            item.severity.upper(),
            item.category,
            item.title,
            ", ".join(item.hostnames),
            item.suggested_prompt,
        )
    console.print(table)
    for item in briefing.items:
        console.print(f"- [{item.severity.upper()}] {item.explanation}")
        if item.suggested_action_prompt:
            console.print(f"  [dim]Action prompt:[/dim] {item.suggested_action_prompt}")


if __name__ == "__main__":
    app()
