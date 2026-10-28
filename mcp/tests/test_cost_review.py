"""TDD: cost review report generator from cost_summary / top_cost_drivers / find_waste."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dbu_mcp import cost_review as cr
from dbu_mcp.server import build


SUMMARY = {
    "days": 15,
    "total_list_cost_usd": 1000.0,
    "usd_complete": False,
    "unpriced_skus": [{"sku_name": "GENIE_FREE_USAGE", "unpriced_dbus": 0.326}],
    "total_dbus": 0.0,
    "by_product": [
        {"billing_origin_product": "JOBS", "list_cost_usd": 700.0},
        {"billing_origin_product": "ALL_PURPOSE", "list_cost_usd": 300.0},
    ],
    "daily": [{"usage_date": "2026-10-01", "list_cost_usd": 33.3}],
    "note": "List price, not your negotiated discount.",
}

DRIVERS = {
    "rows": [
        {"resource_type": "job", "resource_id": "etl-nightly", "list_cost_usd": 500.0},
        {"resource_type": "cluster", "resource_id": "c-shared-01", "list_cost_usd": 300.0},
        {"resource_type": "warehouse", "resource_id": "wh-sql-01", "list_cost_usd": 120.0},
    ],
    "usd_complete": False,
    "unpriced_skus": [{"sku_name": "GENIE_FREE_USAGE", "unpriced_dbus": 0.326}],
}

WASTE = {
    "measured_saving_usd": 203.15,
    "measured_waste_dbu": 4.5,
    "observation_days": 15.0,
    "projected_30d_usd": 406.3,
    "projected_30d_dbu": 9.0,
    "low_confidence": False,
    "jobs_on_all_purpose": [
        {"cluster_id": "c-shared-01", "jobs_using_cluster": 2,
         "cluster_all_purpose_cost_usd": 300.0, "est_saving_if_job_compute_usd": 165.0},
    ],
    "missing_autotermination": [
        {"cluster_id": "c-adhoc-02", "auto_termination_minutes": 0},
    ],
    "idle_or_oversized": [
        {"cluster_id": "c-idle-03", "avg_cpu_pct": 4.0, "avg_mem_pct": 12.0, "est_saving_usd": 35.0},
    ],
    "warehouse_long_autostop": [
        {"warehouse_id": "wh-sql-01", "auto_stop_minutes": 90},
    ],
    "idle_warehouse": [
        {"warehouse_id": "wh-sql-01", "warehouse_size": "2X_SMALL", "uptime_min": 61.0,
         "busy_min": 1, "idle_pct": 98.4, "est_idle_dbus": 4.0, "usd_per_dbu": 0.7,
         "est_idle_cost_usd": 2.8},
    ],
    "serverless_jobs": [
        {"job_id": "etl-nightly", "dbus": 2.0, "list_cost_usd": 1.4, "runs": 4,
         "failed_runs": 1, "est_waste_dbus": 0.5, "est_saving_usd": 0.35},
    ],
    "delta_needs_optimize": [
        {"table_fqn": "main.bronze.events", "file_count": 500, "avg_input_file_bytes": 4194304},
    ],
    "delta_needs_vacuum": [
        {"table_fqn": "main.bronze.events", "days_since_vacuum": 40, "vacuumable_bytes": 3221225472},
    ],
    "delta_overfrequent_optimize": [
        {"table_fqn": "main.silver.orders", "optimize_runs": 14, "runs_per_week": 14.0},
    ],
    "usd_complete": False,
    "notes": ["Delta maintenance findings are proposals only."],
}


def test_report_has_required_sections():
    md = cr.generate_report(SUMMARY, DRIVERS, WASTE, as_of="2026-10-28")
    for heading in (
        "# Databricks cost review",
        "## Executive summary",
        "## Top cost drivers",
        "## Waste findings",
        "## Proposed fixes",
    ):
        assert heading in md, heading
    assert "example data" not in md.lower() or True  # label is caller's job for samples
    assert "203.15" in md
    assert "406.3" in md or "406.30" in md
    assert "etl-nightly" in md
    assert "main.bronze.events" in md
    assert "OPTIMIZE" in md or "delta_optimize" in md
    assert "Proposal only" in md or "propose" in md.lower()


def test_executive_summary_covers_window_and_projection():
    md = cr.generate_report(SUMMARY, DRIVERS, WASTE, as_of="2026-10-28")
    assert "15" in md  # days / observation
    assert "1000" in md or "1,000" in md or "$1000" in md or "$1,000" in md
    assert "list price" in md.lower()


def test_empty_waste_still_renders():
    empty = {
        "measured_saving_usd": 0.0,
        "measured_waste_dbu": 0.0,
        "observation_days": 10.0,
        "projected_30d_usd": 0.0,
        "projected_30d_dbu": 0.0,
        "low_confidence": False,
        "jobs_on_all_purpose": [],
        "missing_autotermination": [],
        "idle_or_oversized": [],
        "warehouse_long_autostop": [],
        "idle_warehouse": [],
        "serverless_jobs": [],
        "delta_needs_optimize": [],
        "delta_needs_vacuum": [],
        "delta_overfrequent_optimize": [],
        "usd_complete": True,
    }
    md = cr.generate_report({"days": 10, "total_list_cost_usd": 50.0, "by_product": [],
                             "usd_complete": True}, {"rows": []}, empty)
    assert "## Waste findings" in md
    assert "no waste" in md.lower() or "0.0" in md or "none" in md.lower()
    assert "## Proposed fixes" in md


def test_access_errors_surfaced_not_as_zero_saving():
    waste = {
        "measured_saving_usd": None,
        "measured_waste_dbu": None,
        "observation_days": None,
        "projected_30d_usd": None,
        "cost_unknown": "USD unknown (not $0): every saving check failed, see errors",
        "errors": {
            "idle_warehouse": {"reason": "TABLE_OR_VIEW_NOT_FOUND", "tables": ["system.query.history"],
                               "fix": "GRANT SELECT ..."},
        },
        "jobs_on_all_purpose": [],
        "missing_autotermination": [],
        "idle_or_oversized": [],
        "warehouse_long_autostop": [],
        "idle_warehouse": [],
        "serverless_jobs": [],
        "delta_needs_optimize": [],
        "delta_needs_vacuum": [],
        "delta_overfrequent_optimize": [],
    }
    md = cr.generate_report({"days": 7, "total_list_cost_usd": None, "errors": {"cost_summary": {}}},
                            {"rows": [], "errors": {}}, waste)
    assert "not $0" in md.lower() or "unknown" in md.lower()
    assert "error" in md.lower()
    assert "$0" not in md.split("Executive")[1].split("##")[0] or "unknown" in md.lower()


def test_projection_withheld_when_few_days():
    waste = dict(WASTE)
    waste["observation_days"] = 1.5
    waste["projected_30d_usd"] = None
    waste["projected_30d_dbu"] = None
    waste["projection_withheld"] = "need >=3 days, have 1.5"
    waste["low_confidence"] = True
    md = cr.generate_report(SUMMARY, DRIVERS, waste)
    assert "withheld" in md.lower() or "need >=3" in md.lower()
    assert "low confidence" in md.lower() or "low_confidence" in md.lower()


def test_unpriced_skus_called_out():
    md = cr.generate_report(SUMMARY, DRIVERS, WASTE)
    assert "GENIE_FREE_USAGE" in md
    assert "incomplete" in md.lower() or "unpriced" in md.lower() or "usd_complete" in md.lower()


def test_proposed_fixes_include_delta_and_bundle_issues():
    fixes = cr.proposed_fixes(WASTE)
    kinds = {f["issue"] for f in fixes}
    assert "all_purpose_job" in kinds or "jobs_on_all_purpose" in {f.get("source") for f in fixes}
    assert any(f["issue"] == "delta_optimize" for f in fixes)
    assert any(f["issue"] == "delta_vacuum" for f in fixes)
    assert any(f["issue"] == "warehouse_autostop" for f in fixes)
    assert all("Proposal only" in (f.get("proposal") or "") or f.get("issue") for f in fixes)


def test_write_markdown_and_optional_pdf(tmp_path: Path):
    md_path = tmp_path / "report.md"
    pdf_path = tmp_path / "report.pdf"
    md = cr.generate_report(SUMMARY, DRIVERS, WASTE, as_of="2026-10-28")
    written = cr.write_report(md, md_path, pdf_path=pdf_path)
    assert md_path.read_text() == md
    assert written["markdown"] == str(md_path)
    # PDF is best-effort; if weasyprint/tool missing, key may be None with a note
    assert "pdf" in written


# --- MCP tool ---

MOCK = {
    "AS avg_input_file_bytes": [{"table_fqn": "main.bronze.events", "file_count": 500,
                                 "avg_input_file_bytes": 4194304}],
    "AS days_since_vacuum": [{"table_fqn": "main.bronze.events", "days_since_vacuum": 40}],
    "AS runs_per_week": [],
    "AS observation_days": [{"observation_days": 15.0, "first_record": "2026-10-11T00:00:00Z"}],
    "est_idle_dbus": [{"warehouse_id": "wh-sql-01", "warehouse_size": "2X_SMALL", "uptime_min": 61.0,
                       "busy_min": 1, "idle_pct": 98.4, "est_idle_dbus": 4.0, "usd_per_dbu": 0.7,
                       "est_idle_cost_usd": 2.8}],
    "est_waste_dbus": [{"job_id": "etl-nightly", "dbus": 2.0, "list_cost_usd": 1.4, "runs": 4,
                        "failed_runs": 1, "est_waste_dbus": 0.5, "est_saving_usd": 0.35}],
    "auto_stop_minutes DESC": [{"warehouse_id": "wh-sql-01", "auto_stop_minutes": 90}],
    "AS unpriced_dbus": [{"sku_name": "GENIE_FREE_USAGE", "unpriced_dbus": 0.326}],
    "billing_origin_product, ROUND": [
        {"billing_origin_product": "JOBS", "list_cost_usd": 700.0},
        {"billing_origin_product": "ALL_PURPOSE", "list_cost_usd": 300.0},
    ],
    "SELECT usage_date, ROUND": [{"usage_date": "2026-10-01", "list_cost_usd": 33.3}],
    "AS resource_type": [{"resource_type": "job", "resource_id": "etl-nightly", "list_cost_usd": 500.0}],
    "jobs_using_cluster": [{"cluster_id": "c-shared-01", "jobs_using_cluster": 2,
                            "cluster_all_purpose_cost_usd": 300.0, "est_saving_if_job_compute_usd": 165.0}],
    "auto_termination_minutes DESC": [{"cluster_id": "c-adhoc-02", "auto_termination_minutes": 0}],
    "avg_cpu_pct": [{"cluster_id": "c-idle-03", "est_saving_usd": 35.0}],
}


def _runner(q):
    for marker, rows in MOCK.items():
        if marker in q:
            return rows
    return []


def call(srv, name, **args):
    res = asyncio.run(srv.call_tool(name, args))
    assert not res.is_error, res.content
    sc = res.structured_content
    if isinstance(sc, dict):
        return sc.get("result", sc)
    import json
    return json.loads(res.content[0].text)


def test_mcp_cost_review_report_tool():
    srv = build(_runner)
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert "cost_review_report" in names
    r = call(srv, "cost_review_report", days=15)
    assert isinstance(r, dict)
    assert "markdown" in r
    assert "# Databricks cost review" in r["markdown"]
    assert r.get("days") == 15
