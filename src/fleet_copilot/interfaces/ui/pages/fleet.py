"""Fleet page: Devices table with inline Device page + personal Saved charts board."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import streamlit as st

from fleet_copilot.interfaces.ui.charts_ui import prepare_chart_spec_for_render
from fleet_copilot.interfaces.ui.humanize import humanize_check_name, humanize_tool_name
from fleet_copilot.interfaces.ui.safe_md import safe_md
from fleet_copilot.interfaces.ui.services import (
    cached_device_detail,
    cached_fleet_devices,
    cached_telemetry_as_of,
    get_resolved_policies,
    get_ui_container,
)
from fleet_copilot.interfaces.ui.state import (
    make_tenant_context,
    open_copilot_thread,
    perf_finish_if_active,
    perf_mark_start,
    queue_copilot_prompt,
    require_session,
    safe_page_run,
    switch_to_page,
)
from fleet_copilot.interfaces.ui.theme import (
    CHART_PALETTE,
    STATUS_HEX,
    fmt_as_of_full,
    fmt_int,
    fmt_pct,
    render_proposal_status_badge,
    render_status_badge,
)
from fleet_copilot.services.demo_auth import DemoSession


def _device_metric_chart_spec(
    series: list[dict[str, Any]],
    *,
    field: str,
    metric_name: str,
    y_title: str,
    policies: dict[str, Any],
) -> dict[str, Any] | None:
    rows = [
        {"date": str(pt["date"]), "value": float(pt[field])}
        for pt in series
        if pt.get(field) is not None
    ]
    if not rows:
        return None
    base: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 170,
        "data": {"values": rows},
        "mark": {"type": "line", "point": False, "strokeWidth": 2, "color": CHART_PALETTE[0]},
        "encoding": {
            "x": {
                "field": "date",
                "type": "temporal",
                "title": None,
                "axis": {"format": "%b %d", "tickCount": 4},
            },
            "y": {
                "field": "value",
                "type": "quantitative",
                "title": y_title,
                "scale": {"zero": field != "battery_cycles"},
                "axis": {"title": y_title, "titlePadding": 8},
            },
            "tooltip": [
                {"field": "date", "type": "temporal", "title": "Date"},
                {"field": "value", "type": "quantitative", "title": y_title},
            ],
        },
    }
    return prepare_chart_spec_for_render(
        base,
        metric=metric_name,
        policies=policies,
        height=170,
        strip_inner_title=True,
    )


def _compliance_timeline_spec(comp_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not comp_rows:
        return None
    # Deduplicate to latest status per (date, check_id)
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for r in comp_rows:
        iso_date = str(r["date"])
        check_id = str(r["check_id"])
        dedup[(iso_date, check_id)] = {
            "date": iso_date[5:] if len(iso_date) == 10 else iso_date,
            "full_date": iso_date,
            "check": humanize_check_name(check_id),
            "status": str(r["status"]),
        }
    vals = sorted(dedup.values(), key=lambda x: x["full_date"])
    n_checks = len({v["check"] for v in vals})
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": max(130, 34 * n_checks + 48),
        "data": {"values": vals},
        "mark": {"type": "rect", "cornerRadius": 3, "stroke": "#FFFFFF", "strokeWidth": 1},
        "encoding": {
            "x": {
                "field": "date",
                "type": "ordinal",
                "sort": None,
                "title": None,
                "axis": {"labelAngle": 0, "labelOverlap": True},
            },
            "y": {"field": "check", "type": "nominal", "title": None},
            "color": {
                "field": "status",
                "type": "nominal",
                "title": "Status",
                "scale": {
                    "domain": ["pass", "fail"],
                    "range": [STATUS_HEX["ok"], STATUS_HEX["high"]],
                },
                "legend": {"orient": "right"},
            },
            "tooltip": [
                {"field": "full_date", "type": "nominal", "title": "Date"},
                {"field": "check", "type": "nominal", "title": "Check"},
                {"field": "status", "type": "nominal", "title": "Status"},
            ],
        },
    }


def _render_device_detail(
    session: DemoSession,
    device_id: str,
    as_of_iso: str,
    policies: dict[str, Any],
) -> None:
    """Render the selected device profile, 30-day charts, compliance, software, and proposals."""
    detail = cached_device_detail(session.company_id, as_of_iso, device_id)
    if detail is None:
        st.warning("Device details are unavailable.", icon=":material/warning:")
        return

    prof = detail["profile"]
    hostname = str(prof.get("os_hostname") or device_id)
    stale_n = detail.get("stale_days_ago")

    with st.container(border=True):
        with st.container(
            horizontal=True,
            horizontal_alignment="distribute",
            vertical_alignment="center",
            gap="small",
        ):
            # Telemetry hostname sanitized before subheader markdown rendering
            st.subheader(safe_md(hostname))
            if st.button(
                "Ask about this device",
                key=f"ask_about_dev_{device_id}",
                type="primary",
                icon=":material/chat:",
                width="content",
            ):
                queue_copilot_prompt(
                    f"Give me a full health summary and recommendations for {hostname}",
                    auto_send=True,
                    new_chat=True,
                )
                switch_to_page("copilot")

        if stale_n is not None:
            render_status_badge("stale", f"Stale · last seen {stale_n} days ago")

        # Profile metadata grid (plain text values)
        p1, p2, p3, p4 = st.columns(4)
        with p1:
            st.caption("Device ID")
            st.text(device_id)
            st.caption("Employee ID")
            st.text(str(prof.get("employee_id") or "—"))
        with p2:
            st.caption("Hardware model")
            model_txt = (
                f"{prof.get('model_name') or 'Unknown'} ({prof.get('model_identifier') or '—'})"
            )
            st.text(model_txt)
            st.caption("Form factor")
            st.text(str(prof.get("form_factor") or "unknown").title())
        with p3:
            st.caption("Operating system")
            os_txt = (
                f"{prof.get('os_product_name') or 'Unknown'} {prof.get('os_product_version') or ''}"
            ).strip()
            st.text(os_txt)
            st.caption("Agent version")
            st.text(str(prof.get("agent_version") or "—"))
        with p4:
            st.caption("Resources")
            ram_gb = prof.get("ram_total_gb")
            disk_gb = prof.get("disk_total_gb")
            res_txt = (
                f"RAM {ram_gb:.0f} GB · Disk {disk_gb:.0f} GB"
                if ram_gb is not None and disk_gb is not None
                else "—"
            )
            st.text(res_txt)
            st.caption("Last seen")
            st.text(fmt_as_of_full(prof.get("collected_at")))

        # 30-day mini charts with dashed threshold lines
        st.markdown("##### 30-day resource trends")
        series = list(detail.get("series") or [])
        ch_cols = st.columns(3, gap="small")
        chart_defs = [
            ("disk_free_pct", "disk_free_pct", "Disk free (%)"),
            ("ram_used_pct", "ram_used_pct", "RAM used (%)"),
            ("battery_cycles", "battery_cycles", "Battery cycles"),
        ]
        for col, (fld, metric_name, y_lbl) in zip(ch_cols, chart_defs, strict=True):
            with col:
                st.caption(y_lbl)
                spec = _device_metric_chart_spec(
                    series,
                    field=fld,
                    metric_name=metric_name,
                    y_title=y_lbl,
                    policies=policies,
                )
                if spec is not None:
                    st.vega_lite_chart(
                        spec=spec,
                        width="stretch",
                        key=f"dev_chart_{device_id}_{fld}",
                    )
                else:
                    st.caption("No telemetry series available.")

        # Compliance timeline
        st.markdown("##### Compliance timeline (30 days)")
        comp_spec = _compliance_timeline_spec(list(detail.get("compliance") or []))
        if comp_spec is not None:
            st.vega_lite_chart(
                spec=comp_spec,
                width="stretch",
                key=f"dev_comp_{device_id}",
            )
        else:
            st.caption("No compliance snapshots recorded in the 30-day window.")

        # Installed software (STRICTLY plain text — never markdown/HTML for injection safety!)
        st.markdown("##### Installed software")
        sw_list = list(detail.get("software") or [])
        if not sw_list:
            st.caption("No installed software inventory recorded.")
        else:
            non_base = [s for s in sw_list if s.get("non_baseline")]
            if non_base:
                st.warning(
                    f"{len(non_base)} non-baseline application(s) detected on this device.",
                    icon=":material/warning:",
                )
                for nb in non_base:
                    # Plain text only (`st.text`) so instruction-like names are never interpreted
                    st.text(f"[Non-baseline] {nb['name']} (v{nb['version']})")

            sw_rows = [
                {
                    "Software": str(s["name"]),
                    "Version": str(s["version"]),
                    "Publisher": str(s["publisher"]),
                    "Baseline": "Non-baseline" if s["non_baseline"] else "Baseline",
                }
                for s in sw_list[:200]
            ]
            st.dataframe(sw_rows, width="stretch", hide_index=True)

        # Open & recent proposals for this device
        st.markdown("##### Proposals for this device")
        container = get_ui_container()
        dev_props = list(container.store.list_proposals(session.company_id, device_id=device_id))
        if not dev_props:
            st.caption("No action proposals recorded for this device.")
        else:
            for p in dev_props[:10]:
                with st.container(border=True):
                    with st.container(
                        horizontal=True,
                        horizontal_alignment="distribute",
                        vertical_alignment="center",
                    ):
                        st.markdown(f"**{humanize_tool_name(str(p.get('tool_name') or ''))}**")
                        render_proposal_status_badge(str(p.get("status") or "pending_approval"))
                    if p.get("rationale"):
                        st.text(str(p["rationale"]))


def _render_devices_tab(
    session: DemoSession,
    as_of_iso: str,
    policies: dict[str, Any],
) -> None:
    """Render filterable Fleet devices table + selectable inline Device page."""

    @st.fragment(key="fleet_devices_frag")
    def _devices_fragment() -> None:
        all_rows = cached_fleet_devices(session.company_id, as_of_iso)
        low_disk_thresh = float(policies.get("low_disk_free_pct", 15.0))

        if not all_rows:
            st.info("No devices found for this company.", icon=":material/laptop_mac:")
            perf_finish_if_active()
            return

        # Filters
        f1, f2, f3 = st.columns([1.8, 1.8, 2.0], gap="small")
        with f1:
            ff_opts = ["All"] + sorted({str(r["form_factor"]) for r in all_rows})
            sel_ff = st.selectbox("Form factor", options=ff_opts, key="fleet_filter_ff")
        with f2:
            os_opts = ["All"] + sorted({str(r["os_family"]) for r in all_rows})
            sel_os = st.selectbox("Operating system", options=os_opts, key="fleet_filter_os")
        with f3:
            st.write("")
            attn_only = st.checkbox("Needs attention only", key="fleet_filter_attn")

        filtered = [
            r
            for r in all_rows
            if (sel_ff == "All" or r["form_factor"] == sel_ff)
            and (sel_os == "All" or r["os_family"] == sel_os)
            and (not attn_only or r["needs_attention"])
        ][:200]

        st.caption(
            f"Showing {fmt_int(len(filtered))} of {fmt_int(len(all_rows))} devices · "
            f"Data as of {fmt_as_of_full(as_of_iso)} · "
            f"Low disk policy: under {fmt_pct(low_disk_thresh)}"
        )

        if not filtered:
            st.info("No devices match the active filters.", icon=":material/filter_list_off:")
            perf_finish_if_active()
            return

        display_rows = [
            {
                "Hostname": r["hostname"],
                "Model": r["model"],
                "OS": r["os"],
                "Form factor": r["form_factor"].title(),
                "Disk free": r["disk_free_pct"],
                "Disk status": "Low" if r["disk_low"] else "OK",
                "RAM used": r["ram_used_pct"],
                "Battery": r["battery"],
                "Failing checks": r["failing_checks"],
                "Last seen": r["last_seen"],
                "Employee": r["employee_id"],
            }
            for r in filtered
        ]

        # Keep progress bars neutral blue so healthy rows aren't painted red; Disk column flags Low
        disk_bar_color = "blue"

        event = st.dataframe(
            display_rows,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="fleet_devices_table",
            column_config={
                "Hostname": st.column_config.TextColumn("Hostname"),
                "Model": st.column_config.TextColumn("Model"),
                "OS": st.column_config.TextColumn("OS"),
                "Form factor": st.column_config.TextColumn("Form factor"),
                "Disk free": st.column_config.ProgressColumn(
                    "Disk free",
                    format="%.1f%%",
                    min_value=0.0,
                    max_value=100.0,
                    color=disk_bar_color,
                    help=f"Devices under {low_disk_thresh:g}% are flagged as Low.",
                ),
                "Disk status": st.column_config.TextColumn("Disk"),
                "RAM used": st.column_config.ProgressColumn(
                    "RAM used",
                    format="%.1f%%",
                    min_value=0.0,
                    max_value=100.0,
                    color="blue",
                ),
                "Battery": st.column_config.TextColumn("Battery"),
                "Failing checks": st.column_config.NumberColumn("Failing checks", format="%d"),
                "Last seen": st.column_config.TextColumn("Last seen"),
                "Employee": st.column_config.TextColumn("Employee"),
            },
        )

        # Sync dataframe row selection with device selector & detail panel (W1)
        sel_rows: list[int] = []
        sel_obj = getattr(event, "selection", None)
        if sel_obj is not None:
            raw_rows = (
                sel_obj.get("rows") if isinstance(sel_obj, dict) else getattr(sel_obj, "rows", None)
            )
            if raw_rows:
                sel_rows = [int(r) for r in raw_rows]

        dev_options = [r["device_id"] for r in filtered]
        host_by_id = {r["device_id"]: r["hostname"] for r in filtered}

        prev_table_rows = st.session_state.get("_fleet_last_table_rows")
        if sel_rows != prev_table_rows:
            st.session_state["_fleet_last_table_rows"] = list(sel_rows)
            if sel_rows and 0 <= sel_rows[0] < len(filtered):
                picked_from_table = filtered[sel_rows[0]]["device_id"]
                perf_mark_start("select_device")
                st.session_state["selected_device_id"] = picked_from_table
                st.session_state["fleet_device_picker"] = picked_from_table

        current_dev = st.session_state.get("selected_device_id")
        if current_dev not in dev_options:
            current_dev = dev_options[0]
            st.session_state["selected_device_id"] = current_dev
        if st.session_state.get("fleet_device_picker") != current_dev:
            st.session_state["fleet_device_picker"] = current_dev

        def _on_pick_device() -> None:
            perf_mark_start("select_device")
            st.session_state["selected_device_id"] = st.session_state.get("fleet_device_picker")

        picked = st.selectbox(
            "Inspect device details",
            options=dev_options,
            format_func=lambda did: f"{host_by_id.get(did, did)} ({did})",
            key="fleet_device_picker",
            on_change=_on_pick_device,
        )
        if picked:
            st.session_state["selected_device_id"] = picked
            _render_device_detail(session, picked, as_of_iso, policies)

        perf_finish_if_active()

    _devices_fragment()


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    cols = list(rows[0].keys())
    writer = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def _render_saved_charts_tab(
    session: DemoSession,
    policies: dict[str, Any],
) -> None:
    """Render personal pinned charts with Refresh, Download (JSON/CSV), Delete, and Open chat."""
    container = get_ui_container()
    ctx = make_tenant_context(session)

    @st.fragment(key="fleet_saved_charts_frag")
    def _charts_fragment() -> None:
        charts = list(container.chart_svc.list_charts(ctx, pinned_only=True, limit=50))
        if not charts:
            st.info(
                "No saved charts yet. Ask Fleet Copilot to chart a metric and click "
                "**Pin to Saved charts** to save it here.",
                icon=":material/show_chart:",
            )
            return

        confirm_id = st.session_state.get("confirm_delete_chart_id")
        for row in charts:
            chart_id = str(row.get("chart_id") or "")
            title = str(row.get("title") or "Saved chart")
            raw_spec = row.get("spec_json")
            data_rows = list(row.get("data_json") or [])
            raw_req = row.get("request_json")
            req_json: dict[str, Any] = raw_req if isinstance(raw_req, dict) else {}
            thread_id = row.get("thread_id")
            as_of = row.get("telemetry_as_of")

            with st.container(border=True):
                with st.container(
                    horizontal=True,
                    horizontal_alignment="distribute",
                    vertical_alignment="center",
                    gap="small",
                ):
                    st.markdown(f"#### {safe_md(title)}")
                    st.caption(f":material/schedule: Data as of {fmt_as_of_full(as_of)}")

                if isinstance(raw_spec, dict):
                    render_spec = prepare_chart_spec_for_render(
                        raw_spec,
                        chart_meta={"title": title, "request_json": req_json},
                        policies=policies,
                        height=260,
                        strip_inner_title=True,
                    )
                    st.vega_lite_chart(
                        spec=render_spec,
                        width="stretch",
                        key=f"saved_chart_vega_{chart_id}",
                    )

                with st.container(horizontal=True, wrap=True, gap="small"):
                    is_sql_chart = req_json.get("kind") == "from_query"
                    if not is_sql_chart:
                        if st.button(
                            "Refresh",
                            key=f"saved_refresh_{chart_id}",
                            icon=":material/refresh:",
                            width="content",
                        ):
                            ctx_ref = make_tenant_context(session)
                            container.chart_svc.refresh_chart(ctx_ref, chart_id)
                            st.toast(f"Refreshed '{title}' on latest telemetry.")
                            st.rerun()
                    else:
                        st.caption("Frozen SQL snapshot")

                    if isinstance(raw_spec, dict):
                        st.download_button(
                            "Download spec (JSON)",
                            data=json.dumps(raw_spec, indent=2, default=str),
                            file_name=f"{chart_id}.vl.json",
                            mime="application/json",
                            key=f"saved_dl_json_{chart_id}",
                            icon=":material/download:",
                        )
                    if data_rows:
                        st.download_button(
                            "Download data (CSV)",
                            data=_rows_to_csv(data_rows),
                            file_name=f"{chart_id}.csv",
                            mime="text/csv",
                            key=f"saved_dl_csv_{chart_id}",
                            icon=":material/table_view:",
                        )

                    if thread_id:
                        if st.button(
                            "Open source chat",
                            key=f"saved_open_chat_{chart_id}",
                            type="tertiary",
                            icon=":material/chat:",
                        ):
                            open_copilot_thread(str(thread_id))

                    if confirm_id == chart_id:
                        if st.button(
                            "Confirm delete",
                            key=f"saved_confirm_del_{chart_id}",
                            type="primary",
                            icon=":material/delete_forever:",
                        ):
                            ctx_del = make_tenant_context(session)
                            container.chart_svc.delete_chart(ctx_del, chart_id)
                            st.session_state.pop("confirm_delete_chart_id", None)
                            st.rerun()
                        if st.button(
                            "Cancel",
                            key=f"saved_cancel_del_{chart_id}",
                            type="tertiary",
                        ):
                            st.session_state.pop("confirm_delete_chart_id", None)
                            st.rerun()
                    else:
                        if st.button(
                            "Delete",
                            key=f"saved_del_{chart_id}",
                            type="tertiary",
                            icon=":material/delete:",
                        ):
                            st.session_state["confirm_delete_chart_id"] = chart_id
                            st.rerun()

    _charts_fragment()


def render_fleet_page() -> None:
    """Render the Fleet page with Devices (+ Device detail) and personal Saved charts."""
    session = require_session()
    as_of_iso = cached_telemetry_as_of(session.company_id)
    policies = get_resolved_policies(session.company_id, as_of_iso)

    st.markdown("## :material/laptop_mac: Fleet")
    st.caption(
        "Inspect device health, 30-day resource trends, compliance history, software inventory, "
        "and your personal saved charts."
    )

    tab_devices, tab_charts = st.tabs(["Devices", "Saved charts"])
    with tab_devices:
        _render_devices_tab(session, as_of_iso, policies)
    with tab_charts:
        _render_saved_charts_tab(session, policies)


def main() -> None:
    safe_page_run(render_fleet_page)


if __name__ == "__main__":
    main()
