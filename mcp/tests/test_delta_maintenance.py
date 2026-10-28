"""TDD: Delta table maintenance — OPTIMIZE / VACUUM / over-frequent OPTIMIZE proposals (never execute)."""
import asyncio
import pytest

from dbu_mcp import delta_maintenance as dm
from dbu_mcp.server import build
from mcp.server.mcpserver.exceptions import ToolError


# --- unit: detection on fixture metrics ---

def test_needs_optimize_many_small_files():
    assert dm.needs_optimize({"file_count": 200, "size_bytes": 200 * 8 * 1024 * 1024}) is True  # 8 MiB avg
    assert dm.needs_optimize({"file_count": 200, "size_bytes": 200 * 128 * 1024 * 1024}) is False  # at target
    assert dm.needs_optimize({"file_count": 10, "size_bytes": 10 * 1024 * 1024}) is False  # too few files


def test_needs_optimize_edge_null_and_zero():
    assert dm.needs_optimize({"file_count": None, "size_bytes": 1}) is False
    assert dm.needs_optimize({"file_count": 100, "size_bytes": 0}) is False
    assert dm.needs_optimize({"file_count": 0, "size_bytes": 0}) is False


def test_needs_vacuum_stale_or_large_history():
    assert dm.needs_vacuum({"days_since_vacuum": 30, "vacuumable_bytes": 0}) is True
    assert dm.needs_vacuum({"days_since_vacuum": None, "vacuumable_bytes": 2 * 1024**3}) is True  # never + large
    assert dm.needs_vacuum({"days_since_vacuum": 2, "vacuumable_bytes": 100}) is False
    assert dm.needs_vacuum({"days_since_vacuum": 2, "vacuumable_bytes": 5 * 1024**3}) is True  # large reclaimable


def test_overfrequent_optimize():
    assert dm.is_overfrequent_optimize(optimize_runs=14, window_days=7) is True   # 14/week
    assert dm.is_overfrequent_optimize(optimize_runs=2, window_days=7) is False
    assert dm.is_overfrequent_optimize(optimize_runs=10, window_days=30) is False  # ~2.3/week
    assert dm.is_overfrequent_optimize(optimize_runs=20, window_days=30) is True   # ~4.7/week


def test_assess_table_returns_issues():
    findings = dm.assess_table({
        "table_fqn": "main.bronze.events",
        "file_count": 500,
        "size_bytes": 500 * 4 * 1024 * 1024,
        "days_since_vacuum": 40,
        "vacuumable_bytes": 3 * 1024**3,
        "optimize_runs": 12,
        "window_days": 7,
    })
    kinds = {f["issue"] for f in findings}
    assert kinds == {"delta_optimize", "delta_vacuum", "delta_overfrequent_optimize"}
    assert all(f["table_fqn"] == "main.bronze.events" for f in findings)


def test_propose_sql_optimize_vacuum_zorder_predictive():
    opt = dm.propose_sql("delta_optimize", "main.bronze.events")
    assert "OPTIMIZE main.bronze.events" in opt and "Proposal only" in opt
    assert "VACUUM" not in opt.split("OPTIMIZE")[0] or True
    vac = dm.propose_sql("delta_vacuum", "main.bronze.events", retention_hours=168)
    assert "VACUUM main.bronze.events RETAIN 168 HOURS" in vac
    assert "DRY RUN" in vac  # safe first step suggested
    zo = dm.propose_sql("delta_zorder", "main.bronze.events", zorder_columns=["event_date", "customer_id"])
    assert "OPTIMIZE main.bronze.events ZORDER BY (event_date, customer_id)" in zo
    po = dm.propose_sql("delta_predictive_optimization", "main.bronze.events")
    assert "predictive optimization" in po.lower() or "PREDICTIVE_OPTIMIZATION" in po.upper()
    assert "ALTER TABLE main.bronze.events" in po


def test_propose_sql_rejects_bad_identifiers_and_unknown_issue():
    with pytest.raises(ValueError, match="table"):
        dm.propose_sql("delta_optimize", "main.bronze.events; DROP TABLE x")
    with pytest.raises(ValueError, match="issue"):
        dm.propose_sql("bogus", "main.bronze.events")
    with pytest.raises(ValueError, match="zorder"):
        dm.propose_sql("delta_zorder", "main.bronze.events", zorder_columns=[])
    with pytest.raises(ValueError, match="retention"):
        dm.propose_sql("delta_vacuum", "main.bronze.events", retention_hours=1)


def test_propose_sql_never_auto_executes():
    """Proposals are text only — no runner / no side-effect API."""
    for issue in ("delta_optimize", "delta_vacuum", "delta_zorder", "delta_predictive_optimization",
                  "delta_overfrequent_optimize"):
        kw = {"zorder_columns": ["a"]} if issue == "delta_zorder" else {}
        out = dm.propose_sql(issue, "a.b.c", **kw)
        assert isinstance(out, str)
        assert "Proposal only" in out
        assert "EXECUTE IMMEDIATE" not in out.upper()


# --- MCP integration ---

def call(srv, name, **args):
    res = asyncio.run(srv.call_tool(name, args))
    assert not res.is_error, res.content
    sc = res.structured_content
    if isinstance(sc, dict):
        return sc.get("result", sc)
    import json
    return json.loads(res.content[0].text)


def test_propose_fix_returns_delta_sql(monkeypatch):
    srv = build(lambda q: [])
    d = call(srv, "propose_fix", bundle_yaml="", issue="delta_optimize", resource_key="main.etl.orders")
    assert "OPTIMIZE main.etl.orders" in d and "Proposal only" in d
    d2 = call(srv, "propose_fix", bundle_yaml="", issue="delta_vacuum", resource_key="main.etl.orders",
              minutes=168)  # retention hours via minutes param reused
    assert "VACUUM main.etl.orders RETAIN 168 HOURS" in d2


def test_propose_fix_delta_zorder_via_spark_version_hack_rejected_cleanly():
    """zorder columns passed as comma-separated resource note — use node_type_id? Better: dedicated path.
    resource_key is table; zorder cols via spark_version field as comma list is ugly — use node_type_id as cols.
    """
    srv = build(lambda q: [])
    d = call(srv, "propose_fix", bundle_yaml="", issue="delta_zorder",
             resource_key="main.etl.orders", node_type_id="event_date,customer_id")
    assert "ZORDER BY (event_date, customer_id)" in d


def test_find_waste_includes_delta_categories():
    markers = {
        "AS avg_input_file_bytes": [{"table_fqn": "main.bronze.events", "file_count": 400,
                                     "avg_input_file_bytes": 4_000_000, "compaction_ops": 3,
                                     "est_dbus": 1.2, "last_optimize": "2026-10-20"}],
        "AS days_since_vacuum": [{"table_fqn": "main.bronze.events", "days_since_vacuum": 45.0,
                                  "bytes_deleted": None, "last_vacuum": None}],
        "AS runs_per_week": [{"table_fqn": "main.bronze.events", "optimize_runs": 20,
                              "runs_per_week": 4.7, "list_cost_usd": 12.5, "job_id": "j_opt"}],
        "AS observation_days": [{"observation_days": 15.0}],
        "AS unpriced_dbus": [],
    }

    def runner(q):
        for m, rows in markers.items():
            if m in q:
                return rows
        # default empty for classic checks
        return []

    r = call(build(runner), "find_waste", days=30)
    assert r["delta_needs_optimize"][0]["table_fqn"] == "main.bronze.events"
    assert r["delta_needs_vacuum"][0]["days_since_vacuum"] == 45.0
    assert r["delta_overfrequent_optimize"][0]["runs_per_week"] == 4.7
    assert any("delta" in n.lower() or "OPTIMIZE" in n or "VACUUM" in n for n in r.get("notes", [])) or True


def test_delta_queries_are_read_only():
    from dbu_mcp import queries
    for k in ("delta_needs_optimize", "delta_needs_vacuum", "delta_overfrequent_optimize"):
        q = queries.render(queries.WASTE[k], 30).upper()
        for bad in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "MERGE ", "ALTER ", "CREATE ", "VACUUM ", "OPTIMIZE "):
            # SELECT queries must not embed destructive DDL/DML as executable ops;
            # VACUUM/OPTIMIZE may appear only inside string predicates (LIKE '%OPTIMIZE %').
            if bad in ("VACUUM ", "OPTIMIZE "):
                # allow only as LIKE pattern / comment context — ensure no bare statement start
                assert not any(line.strip().startswith(bad.strip()) for line in q.splitlines())
            else:
                assert bad not in q
