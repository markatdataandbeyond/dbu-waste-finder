"""Cost review report: one markdown (and optional PDF) from cost_summary / drivers / find_waste.

Proposals only — nothing is executed against the workspace.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import delta_maintenance as dm

WASTE_CHECKS = (
    "jobs_on_all_purpose",
    "missing_autotermination",
    "idle_or_oversized",
    "warehouse_long_autostop",
    "idle_warehouse",
    "serverless_jobs",
    "delta_needs_optimize",
    "delta_needs_vacuum",
    "delta_overfrequent_optimize",
)

_CHECK_ISSUE = {
    "jobs_on_all_purpose": "all_purpose_job",
    "missing_autotermination": "no_autotermination",
    "warehouse_long_autostop": "warehouse_autostop",
    "idle_warehouse": "warehouse_autostop",
    "delta_needs_optimize": "delta_optimize",
    "delta_needs_vacuum": "delta_vacuum",
    "delta_overfrequent_optimize": "delta_overfrequent_optimize",
}


def _money(v) -> str:
    if v is None:
        return "unknown (not $0)"
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "unknown (not $0)"


def _as_of(as_of: str | None) -> str:
    if as_of:
        return as_of
    return datetime.now(timezone.utc).date().isoformat()


def proposed_fixes(find_waste: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive reviewable fix proposals from find_waste rows (text only)."""
    out: list[dict[str, Any]] = []
    for row in find_waste.get("jobs_on_all_purpose") or []:
        cid = row.get("cluster_id")
        out.append({
            "issue": "all_purpose_job",
            "source": "jobs_on_all_purpose",
            "resource": cid,
            "est_saving_usd": row.get("est_saving_if_job_compute_usd"),
            "proposal": (
                "-- Proposal only; review before applying. DBU Waste Finder never applies this.\n"
                f"-- Move jobs off all-purpose cluster {cid} onto a job cluster "
                "(propose_fix issue='all_purpose_job').\n"
            ),
        })
    for row in find_waste.get("missing_autotermination") or []:
        cid = row.get("cluster_id")
        out.append({
            "issue": "no_autotermination",
            "source": "missing_autotermination",
            "resource": cid,
            "est_saving_usd": None,
            "proposal": (
                "-- Proposal only; review before applying. DBU Waste Finder never applies this.\n"
                f"-- Set autotermination_minutes on cluster {cid} "
                "(propose_fix issue='no_autotermination').\n"
            ),
        })
    for check in ("warehouse_long_autostop", "idle_warehouse"):
        for row in find_waste.get(check) or []:
            wid = row.get("warehouse_id")
            out.append({
                "issue": "warehouse_autostop",
                "source": check,
                "resource": wid,
                "est_saving_usd": row.get("est_idle_cost_usd"),
                "proposal": (
                    "-- Proposal only; review before applying. DBU Waste Finder never applies this.\n"
                    f"-- Set auto_stop_mins on SQL warehouse {wid} "
                    "(propose_fix issue='warehouse_autostop').\n"
                ),
            })
    for row in find_waste.get("idle_or_oversized") or []:
        cid = row.get("cluster_id")
        out.append({
            "issue": "idle_or_oversized",
            "source": "idle_or_oversized",
            "resource": cid,
            "est_saving_usd": row.get("est_saving_usd"),
            "proposal": (
                "-- Proposal only; review before applying. DBU Waste Finder never applies this.\n"
                f"-- Right-size or stop idle/oversized cluster {cid} "
                f"(est. saving {_money(row.get('est_saving_usd'))} in the window).\n"
            ),
        })
    for row in find_waste.get("serverless_jobs") or []:
        if not row.get("failed_runs"):
            continue
        jid = row.get("job_id")
        out.append({
            "issue": "serverless_failed_runs",
            "source": "serverless_jobs",
            "resource": jid,
            "est_saving_usd": row.get("est_saving_usd"),
            "proposal": (
                "-- Proposal only; review before applying. DBU Waste Finder never applies this.\n"
                f"-- Investigate failed runs on serverless job {jid} "
                f"(est. waste {_money(row.get('est_saving_usd'))} / "
                f"{row.get('est_waste_dbus')} DBU in the window).\n"
            ),
        })
    for row in find_waste.get("delta_needs_optimize") or []:
        table = row.get("table_fqn") or row.get("table")
        if not table:
            continue
        try:
            proposal = dm.propose_sql("delta_optimize", table)
        except ValueError as e:
            proposal = f"-- Proposal only; skipped unsafe table name: {e}\n"
        out.append({
            "issue": "delta_optimize", "source": "delta_needs_optimize",
            "resource": table, "est_saving_usd": None, "proposal": proposal,
        })
    for row in find_waste.get("delta_needs_vacuum") or []:
        table = row.get("table_fqn") or row.get("table")
        if not table:
            continue
        try:
            proposal = dm.propose_sql("delta_vacuum", table)
        except ValueError as e:
            proposal = f"-- Proposal only; skipped unsafe table name: {e}\n"
        out.append({
            "issue": "delta_vacuum", "source": "delta_needs_vacuum",
            "resource": table, "est_saving_usd": None, "proposal": proposal,
        })
    for row in find_waste.get("delta_overfrequent_optimize") or []:
        table = row.get("table_fqn") or row.get("table")
        if not table:
            continue
        try:
            proposal = dm.propose_sql("delta_overfrequent_optimize", table)
        except ValueError as e:
            proposal = f"-- Proposal only; skipped unsafe table name: {e}\n"
        out.append({
            "issue": "delta_overfrequent_optimize", "source": "delta_overfrequent_optimize",
            "resource": table, "est_saving_usd": None, "proposal": proposal,
        })
    return out


def _waste_rows(find_waste: dict[str, Any]) -> list[tuple[str, dict]]:
    rows = []
    for check in WASTE_CHECKS:
        for r in find_waste.get(check) or []:
            rows.append((check, r))
    return rows


def generate_report(cost_summary: dict[str, Any], top_cost_drivers: dict[str, Any],
                    find_waste: dict[str, Any], *, as_of: str | None = None) -> str:
    """Return a markdown cost-review report. List price; proposals only."""
    when = _as_of(as_of)
    days = cost_summary.get("days") or find_waste.get("observation_days") or "?"
    total = cost_summary.get("total_list_cost_usd")
    measured = find_waste.get("measured_saving_usd")
    measured_dbu = find_waste.get("measured_waste_dbu")
    obs = find_waste.get("observation_days")
    proj = find_waste.get("projected_30d_usd")
    proj_dbu = find_waste.get("projected_30d_dbu")
    lines: list[str] = []
    lines.append("# Databricks cost review")
    lines.append("")
    lines.append(f"_Generated {when}. Costs are **list price**, not your negotiated discount. "
                 "Fixes are proposals only — nothing is applied or executed._")
    lines.append("")

    # Executive summary
    lines.append("## Executive summary")
    lines.append("")
    lines.append(f"- Analysis window: **{days}** day(s) requested"
                 + (f"; **{obs:g}** observed day(s) of data" if isinstance(obs, (int, float)) else "")
                 + ".")
    lines.append(f"- Total list-price cost in window: **{_money(total)}**.")
    if cost_summary.get("usd_complete") is False or find_waste.get("usd_complete") is False:
        lines.append("- USD totals are **incomplete** (some SKUs or items are unpriced); see unpriced notes below.")
    if find_waste.get("cost_unknown"):
        lines.append(f"- Measured saving: **unknown (not $0)** — {find_waste['cost_unknown']}")
    else:
        lines.append(f"- Measured waste / saving in window: **{_money(measured)}**"
                     + (f" / **{measured_dbu} DBU**" if measured_dbu is not None else "") + ".")
    if find_waste.get("projection_withheld"):
        lines.append(f"- 30-day projection **withheld**: {find_waste['projection_withheld']}.")
    else:
        lines.append(f"- Projected 30-day waste (extrapolation): **{_money(proj)}**"
                     + (f" / **{proj_dbu} DBU**" if proj_dbu is not None else "") + ".")
    if find_waste.get("low_confidence"):
        lines.append("- **Low confidence**: fewer than 7 observed days (or observation unknown).")
    unpriced = list(cost_summary.get("unpriced_skus") or []) + list(top_cost_drivers.get("unpriced_skus") or [])
    # dedupe by sku_name
    seen = set()
    uniq = []
    for u in unpriced:
        name = u.get("sku_name") if isinstance(u, dict) else str(u)
        if name in seen:
            continue
        seen.add(name)
        uniq.append(u)
    if uniq:
        names = ", ".join(
            (f"{u.get('sku_name')} ({u.get('unpriced_dbus')} DBU)" if isinstance(u, dict) else str(u))
            for u in uniq
        )
        lines.append(f"- Unpriced SKUs (USD incomplete): {names}.")
    products = cost_summary.get("by_product") or []
    if products:
        lines.append("- By product: " + ", ".join(
            f"{p.get('billing_origin_product')} {_money(p.get('list_cost_usd'))}" for p in products
        ) + ".")
    lines.append("")

    # Top drivers
    lines.append("## Top cost drivers")
    lines.append("")
    drivers = top_cost_drivers.get("rows") or []
    if not drivers:
        lines.append("_No cost-driver rows returned._")
    else:
        lines.append("| Type | Resource | List-price USD |")
        lines.append("|---|---|---|")
        for r in drivers:
            lines.append(
                f"| {r.get('resource_type', '')} | {r.get('resource_id', '')} | {_money(r.get('list_cost_usd'))} |"
            )
    lines.append("")

    # Waste findings
    lines.append("## Waste findings")
    lines.append("")
    findings = _waste_rows(find_waste)
    if not findings:
        lines.append("No waste findings in this window (measured saving 0.0 or empty checks).")
    else:
        lines.append("| Check | Resource | Est. saving (window) | Detail |")
        lines.append("|---|---|---|---|")
        for check, r in findings:
            resource = (
                r.get("table_fqn") or r.get("cluster_id") or r.get("warehouse_id")
                or r.get("job_id") or r.get("resource_id") or ""
            )
            saving = (
                r.get("est_saving_if_job_compute_usd")
                if check == "jobs_on_all_purpose"
                else r.get("est_idle_cost_usd") if check == "idle_warehouse"
                else r.get("est_saving_usd")
            )
            detail_bits = []
            for k in ("jobs_using_cluster", "auto_termination_minutes", "auto_stop_minutes",
                      "idle_pct", "avg_cpu_pct", "failed_runs", "file_count",
                      "days_since_vacuum", "runs_per_week", "optimize_runs"):
                if r.get(k) is not None:
                    detail_bits.append(f"{k}={r.get(k)}")
            lines.append(
                f"| `{check}` | {resource} | {_money(saving) if saving is not None else '—'} | "
                f"{'; '.join(detail_bits) or '—'} |"
            )
    errors = find_waste.get("errors") or {}
    if errors:
        lines.append("")
        lines.append("### Errors (checks that could not run)")
        lines.append("")
        for k, err in errors.items():
            if isinstance(err, dict):
                lines.append(
                    f"- `{k}`: {err.get('reason', 'error')} — {err.get('fix', '')} "
                    f"(tables: {', '.join(err.get('tables') or [])})"
                )
            else:
                lines.append(f"- `{k}`: {err}")
        lines.append("")
        lines.append("_Failed checks mean totals cover only what ran; missing access is not a $0 finding._")
    lines.append("")

    # Proposed fixes
    lines.append("## Proposed fixes")
    lines.append("")
    fixes = proposed_fixes(find_waste)
    if not fixes:
        lines.append("No fix proposals (no waste rows to act on).")
    else:
        for i, f in enumerate(fixes, 1):
            lines.append(f"### {i}. `{f['issue']}` — {f.get('resource')}")
            if f.get("est_saving_usd") is not None:
                lines.append(f"- Estimated saving in window: {_money(f['est_saving_usd'])}")
            lines.append("")
            lines.append("```sql")
            lines.append((f.get("proposal") or "").rstrip())
            lines.append("```")
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_DBU Waste Finder · Data and Beyond · MIT. Not affiliated with Databricks, Inc._")
    lines.append("")
    return "\n".join(lines)


def write_report(markdown: str, md_path: str | Path, *, pdf_path: str | Path | None = None) -> dict[str, Any]:
    """Write markdown; optionally try PDF via weasyprint. Never raises on PDF failure."""
    md_path = Path(md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    out: dict[str, Any] = {"markdown": str(md_path), "pdf": None}
    if pdf_path is None:
        return out
    pdf_path = Path(pdf_path)
    try:
        import html as _html
        import shutil
        import subprocess
        import tempfile
        html = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<style>body{font-family:system-ui,sans-serif;font-size:11pt;line-height:1.4;"
            "max-width:800px;margin:24px auto;padding:0 16px}"
            "pre{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:9.5pt}</style>"
            f"</head><body><pre>{_html.escape(markdown)}</pre></body></html>"
        )
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from weasyprint import HTML  # type: ignore
            HTML(string=html).write_pdf(str(pdf_path))
        except Exception:
            cli = shutil.which("weasyprint")
            if not cli:
                raise
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
                fh.write(html)
                html_file = fh.name
            try:
                subprocess.run([cli, html_file, str(pdf_path)], check=True, capture_output=True, text=True)
            finally:
                Path(html_file).unlink(missing_ok=True)
        out["pdf"] = str(pdf_path)
    except Exception as e:  # best-effort
        out["pdf"] = None
        out["pdf_note"] = f"PDF skipped: {type(e).__name__}: {e}"
    return out
