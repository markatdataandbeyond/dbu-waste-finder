import asyncio
import yaml
import pytest

from dbu_mcp import queries, fixes, _vendored
from dbu_mcp.server import build

MOCK = {
    "AS observation_days": [{"observation_days": 15.0, "first_record": "2026-10-11T00:00:00Z"}],
    "est_idle_dbus": [{"warehouse_id": "wh1", "warehouse_size": "2X_SMALL", "uptime_min": 61.0, "busy_min": 1,
                       "idle_pct": 98.4, "est_idle_dbus": 4.0, "usd_per_dbu": 0.7, "est_idle_cost_usd": 2.8}],
    "est_waste_dbus": [{"job_id": "j7", "dbus": 2.0, "list_cost_usd": 1.4, "runs": 4, "failed_runs": 1,
                        "est_waste_dbus": 0.5, "est_saving_usd": 0.35},
                       {"job_id": "j8", "dbus": 1.0, "list_cost_usd": 0.7, "runs": 2, "failed_runs": 0,
                        "est_waste_dbus": 0.0, "est_saving_usd": 0.0}],
    "auto_stop_minutes DESC": [{"warehouse_id": "wh1", "auto_stop_minutes": 90}],
    "AS unpriced_dbus": [{"sku_name": "GENIE_FREE_USAGE", "unpriced_dbus": 0.326}],
    "billing_origin_product, ROUND": [{"billing_origin_product": "JOBS", "list_cost_usd": 700.0},
                                       {"billing_origin_product": "ALL_PURPOSE", "list_cost_usd": 300.0}],
    "SELECT usage_date, ROUND": [{"usage_date": "2026-10-01", "list_cost_usd": 33.3}],
    "AS resource_type": [{"resource_type": "job", "resource_id": "123", "list_cost_usd": 500.0}],
    "jobs_using_cluster": [{"cluster_id": "c1", "jobs_using_cluster": 2, "cluster_all_purpose_cost_usd": 300.0,
                            "est_saving_if_job_compute_usd": 165.0}],
    "auto_termination_minutes DESC": [{"cluster_id": "c2", "auto_termination_minutes": 0}],
    "avg_cpu_pct": [{"cluster_id": "c3", "est_saving_usd": 35.0}],
    "delta_usd": [{"resource_id": "123", "day_cost_usd": 90.0, "baseline_daily_usd": 30.0, "delta_usd": 60.0},
                  {"resource_id": "w1", "day_cost_usd": 5.0, "baseline_daily_usd": 10.0, "delta_usd": -5.0}],
}
seen = []


def runner(q):
    seen.append(q)
    for marker, rows in MOCK.items():
        if marker in q:
            return rows
    raise AssertionError("unexpected query:\n" + q)


def call(srv, name, **args):
    res = asyncio.run(srv.call_tool(name, args))
    assert not res.is_error, res.content
    sc = res.structured_content
    if isinstance(sc, dict):
        return sc.get("result", sc)
    import json
    txt = res.content[0].text
    try:
        return json.loads(txt)
    except ValueError:
        return txt


@pytest.fixture
def srv():
    seen.clear()
    return build(runner)


def test_tools_listed(srv):
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names == {"cost_summary", "top_cost_drivers", "find_waste", "explain_cost_spike", "propose_fix"}


def test_cost_summary(srv):
    r = call(srv, "cost_summary", days=30)
    assert r["total_list_cost_usd"] == 1000.0 and r["daily"][0]["list_cost_usd"] == 33.3


def test_top_cost_drivers(srv):
    r = call(srv, "top_cost_drivers")
    assert r["rows"][0]["resource_id"] == "123" and r["unpriced_skus"][0]["sku_name"] == "GENIE_FREE_USAGE"


def test_find_waste_saving_scaled_to_month(srv):
    r = call(srv, "find_waste", days=15)
    assert r["measured_saving_usd"] == 203.15     # 165+35+0.35 failed run+2.8 idle warehouse, measured
    assert r["measured_waste_dbu"] == 4.5          # 4 idle wh + 0.5 failed run
    assert r["observation_days"] == 15.0           # from data coverage, not the days=15 argument
    assert r["projected_30d_usd"] == 406.3 and r["projected_30d_dbu"] == 9.0 and r["low_confidence"] is False
    assert "est_monthly_saving_usd" not in r
    assert r["warehouse_long_autostop"][0]["auto_stop_minutes"] == 90
    assert r["idle_warehouse"][0]["warehouse_id"] == "wh1"
    assert r["missing_autotermination"][0]["cluster_id"] == "c2"
    assert "errors" not in r


def test_find_waste_degrades_when_schema_missing():
    def r2(q):
        if "node_timeline" in q:
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND system.compute.node_timeline")
        return runner(q)
    r = call(build(r2), "find_waste")
    assert "idle_or_oversized" in r["errors"] and r["measured_saving_usd"] == 168.15


def test_explain_cost_spike(srv):
    r = call(srv, "explain_cost_spike", date="2026-10-20")
    assert r["net_delta_top15_usd"] == 55.0
    assert "DATE'2026-10-20'" in seen[-1]


def test_spike_rejects_injection():
    with pytest.raises(ValueError):
        queries.render_spike("2026-10-20'; DROP TABLE x; --")


def test_all_queries_read_only(srv):
    call(srv, "cost_summary"); call(srv, "find_waste"); call(srv, "explain_cost_spike", date="2026-10-01")
    for q in seen:
        up = q.upper()
        for kw in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "MERGE ", "ALTER ", "CREATE "):
            assert kw not in up
        assert "system." in q


def test_queries_reused_from_notebook():
    assert queries.PRICED_USAGE == _vendored.PRICED_USAGE
    assert queries.WASTE == _vendored.WASTE and queries.COST_BY_PRODUCT == _vendored.COST_BY_PRODUCT


BUNDLE = """
bundle: {name: demo}
resources:
  jobs:
    nightly_etl:
      name: nightly-etl
      tasks:
        - task_key: ingest
          existing_cluster_id: 0101-abc
          notebook_task: {notebook_path: ./ingest.py}
  clusters:
    shared_dev:
      cluster_name: shared-dev
      spark_version: 15.4.x-scala2.12
      node_type_id: i3.xlarge
      num_workers: 2
"""


def test_propose_fix_all_purpose(srv):
    d = call(srv, "propose_fix", bundle_yaml=BUNDLE, issue="all_purpose_job", resource_key="nightly_etl",
             node_type_id="i3.xlarge")
    minus = [l.strip() for l in d.splitlines() if l.startswith("-") and not l.startswith("---")]
    plus = [l[1:].strip() for l in d.splitlines() if l.startswith("+") and not l.startswith("+++")]
    assert any("existing_cluster_id: 0101-abc" in m for m in minus)
    assert "job_cluster_key: nightly_etl_job_cluster" in plus and "node_type_id: i3.xlarge" in plus
    assert d.startswith("--- a/databricks.yml")


def test_propose_fix_autotermination_and_result_is_valid_yaml():
    d = fixes.propose(BUNDLE, "no_autotermination", "shared_dev", minutes=20)
    assert "+      autotermination_minutes: 20" in d
    new = fixes.fix_autotermination(yaml.safe_load(BUNDLE), "shared_dev", 20)
    assert yaml.safe_load(fixes._dump(new))["resources"]["clusters"]["shared_dev"]["autotermination_minutes"] == 20


def test_propose_fix_errors():
    with pytest.raises(KeyError):
        fixes.propose(BUNDLE, "all_purpose_job", "missing")
    with pytest.raises(ValueError):
        fixes.propose(BUNDLE, "bogus", "nightly_etl")


# --- missing prices: known partial sum + usd_complete flag + list of what is unpriced (never null total) ---
def test_mixed_prices_partial_sums_everywhere():
    def r3(q):
        if "AS unpriced_dbus" in q:
            return [{"sku_name": "GENIE_FREE_USAGE", "unpriced_dbus": 0.326}]
        if "billing_origin_product, ROUND" in q:
            return [{"billing_origin_product": "SQL", "dbus": 0.533, "list_cost_usd": 0.37, "usd_complete": True},
                    {"billing_origin_product": "GENIE", "dbus": 0.326, "list_cost_usd": None, "usd_complete": False}]
        if "SELECT usage_date, ROUND" in q:
            return [{"usage_date": "2026-10-26", "dbus": 0.859, "list_cost_usd": 0.37, "usd_complete": False}]
        if "AS resource_type" in q:
            return [{"resource_type": "warehouse", "resource_id": "wh", "dbus": 0.533, "list_cost_usd": 0.37, "usd_complete": True}]
        if "est_idle_dbus" in q:
            return [{"warehouse_id": "a", "est_idle_dbus": 3.93, "est_idle_cost_usd": 2.75},
                    {"warehouse_id": "b", "est_idle_dbus": 5.0, "est_idle_cost_usd": None}]
        if "est_waste_dbus" in q:
            return [{"job_id": "j7", "dbus": 2.0, "list_cost_usd": None, "runs": 4, "failed_runs": 1,
                     "est_waste_dbus": 0.5, "est_saving_usd": None}]
        if "jobs_using_cluster" in q or "avg_cpu_pct" in q:
            return []
        if "delta_usd" in q:
            return [{"resource_id": "wh", "delta_dbus": 0.533, "delta_usd": 0.37, "usd_complete": True},
                    {"resource_id": "GENIE", "delta_dbus": 0.326, "delta_usd": None, "usd_complete": False}]
        return runner(q)
    srv = build(r3)
    cs = call(srv, "cost_summary")
    assert cs["total_list_cost_usd"] == 0.37 and cs["usd_complete"] is False
    assert cs["unpriced_skus"] == [{"sku_name": "GENIE_FREE_USAGE", "unpriced_dbus": 0.326}]
    fw = call(srv, "find_waste", days=30)
    assert fw["measured_saving_usd"] == 2.75 and fw["usd_complete"] is False
    assert fw["projected_30d_usd"] == 5.5          # 2.75 / 15 obs days * 30
    assert {"check": "idle_warehouse", "id": "b"} in fw["unpriced_items"]
    assert {"check": "serverless_jobs", "id": "j7"} in fw["unpriced_items"]
    assert fw["unpriced_skus"][0]["sku_name"] == "GENIE_FREE_USAGE"
    assert fw["measured_waste_dbu"] == 9.43
    sp = call(srv, "explain_cost_spike", date="2026-10-26")
    assert sp["net_delta_top15_usd"] == 0.37 and sp["usd_complete"] is False and sp["net_delta_top15_dbus"] == 0.859
    tc = call(srv, "top_cost_drivers")
    assert tc["usd_complete"] is False  # rows complete, but an unpriced SKU exists in the window


def test_all_priced_is_complete(srv):
    def r5(q):
        return [] if "AS unpriced_dbus" in q else runner(q)
    cs = call(build(r5), "cost_summary")
    assert cs["usd_complete"] is True and cs["unpriced_skus"] == []


@pytest.mark.parametrize("tool,extra", [("cost_summary", {}), ("top_cost_drivers", {}), ("find_waste", {})])
@pytest.mark.parametrize("bad", [0, -1, 100000])
def test_invalid_days_one_clear_error(tool, extra, bad):
    from mcp.server.mcpserver.exceptions import ToolError
    qs = []
    with pytest.raises(ToolError, match="days must be an integer 1..365"):
        asyncio.run(build(lambda q: qs.append(q) or []).call_tool(tool, {"days": bad, **extra}))
    assert qs == []


def test_non_integer_days_rejected():
    res = None
    try:
        res = asyncio.run(build(lambda q: []).call_tool("find_waste", {"days": 1.5}))
    except Exception as e:
        assert "days" in str(e)
        return
    assert res.is_error and "days" in res.content[0].text


def test_serverless_queries_rendered(srv):
    call(srv, "find_waste", max_autotermination_minutes=45, warehouse_idle_pct=70)
    wh = [q for q in seen if "system.compute.warehouses" in q and "auto_stop_minutes DESC" in q][0]
    assert "auto_stop_minutes > 45" in wh and "auto_stop_minutes = 0" in wh
    idle = [q for q in seen if "est_idle_dbus" in q][0]
    assert "system.compute.warehouse_events" in idle and "system.query.history" in idle and ">= 70.0" in idle
    assert any("system.lakeflow.job_run_timeline" in q and "SERVERLESS" in q for q in seen)


WH_BUNDLE = """
resources:
  sql_warehouses:
    bi:
      name: bi
      cluster_size: 2X-Small
      auto_stop_mins: 90
"""


def test_propose_fix_warehouse_autostop(srv):
    d = call(srv, "propose_fix", bundle_yaml=WH_BUNDLE, issue="warehouse_autostop", resource_key="bi")
    assert "-      auto_stop_mins: 90" in d and "+      auto_stop_mins: 10" in d
    with pytest.raises(ValueError):
        fixes.propose(WH_BUNDLE, "warehouse_autostop", "bi", minutes=0 or 999)
    with pytest.raises(KeyError):
        fixes.propose(WH_BUNDLE, "warehouse_autostop", "nope")


# --- missing access: concrete per-check error, never a fake $0 saving ---
def test_find_waste_no_access_gives_concrete_errors_not_zero():
    def deny(q):
        if "system.compute" in q:
            raise RuntimeError("[INSUFFICIENT_PERMISSIONS] User does not have SELECT on Table 'system.compute.clusters'.")
        raise RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] The table or view `system`.`billing`.`usage` cannot be found.")
    r = call(build(deny), "find_waste")
    assert r["measured_saving_usd"] is None and r["measured_waste_dbu"] is None  # checks failed: unknown
    assert r["projected_30d_usd"] is None and r["low_confidence"] is True
    assert set(r["errors"]) == {"jobs_on_all_purpose", "missing_autotermination", "idle_or_oversized",
                                "warehouse_long_autostop", "idle_warehouse", "serverless_jobs", "unpriced_skus", "observation"}
    e = r["errors"]["warehouse_long_autostop"]
    assert e["reason"] == "permission_denied" and e["tables"] == ["system.compute.warehouses"]
    assert "GRANT SELECT ON TABLE system.compute.warehouses" in e["fix"]
    e = r["errors"]["serverless_jobs"]
    assert e["reason"] == "table_not_found" and "system.billing.usage" in e["tables"]
    assert "system schema" in e["fix"]
    assert "jobs_on_all_purpose" not in r  # failed checks are not shown as empty = "no waste"


def test_find_waste_row_limit():
    many = [{"cluster_id": f"c{i}", "est_saving_usd": 1.0} for i in range(500)]
    qs = []
    def r4(q):
        qs.append(q)
        return many if "avg_cpu_pct" in q else []
    r = call(build(r4), "find_waste", max_rows=25)
    assert len(r["idle_or_oversized"]) == 25 and r["truncated"] == {"idle_or_oversized": {"shown": 25, "total": 500}}
    assert r["measured_saving_usd"] == 500.0      # totals over ALL 500 rows, not the 25 shown
    assert r["totals_partial"] is False
    assert all(q.rstrip().endswith("LIMIT 100001") for q in qs if "unpriced_dbus" not in q and "observation_days" not in q)  # safety cap only


def test_find_waste_totals_marked_partial_when_safety_cap_hit(monkeypatch):
    from dbu_mcp import server
    monkeypatch.setattr(server, "FETCH_CAP", 10)
    many = [{"cluster_id": f"c{i}", "est_saving_usd": 1.0} for i in range(11)]
    r = call(build(lambda q: many if "avg_cpu_pct" in q else []), "find_waste", max_rows=5)
    assert r["totals_partial"] is True and r["truncated"]["idle_or_oversized"]["total"] == ">10"
    assert any("partial" in n for n in r["notes"])


DENY = "[INSUFFICIENT_PERMISSIONS] User does not have SELECT on Table 'system.billing.usage'."


@pytest.mark.parametrize("tool,args", [("cost_summary", {}), ("top_cost_drivers", {}),
                                       ("explain_cost_spike", {"date": "2026-10-26"})])
def test_other_tools_give_concrete_access_error(tool, args):
    def deny(q):
        raise RuntimeError(DENY)
    r = call(build(deny), tool, **args)
    e = r["errors"][tool]
    assert e["reason"] == "permission_denied" and e["tables"] == ["system.billing.usage"]
    assert "GRANT SELECT ON TABLE system.billing.usage" in e["fix"]


def test_string_booleans_from_statement_api():
    """Statement API returns booleans as 'true'/'false' strings (seen live 2026-10-26)."""
    def r6(q):
        if "AS unpriced_dbus" in q:
            return [{"sku_name": "GENIE_FREE_USAGE", "unpriced_dbus": "0.326"}]
        if "delta_usd" in q:
            return [{"resource_id": "wh", "delta_dbus": "0.533", "delta_usd": "0.37", "usd_complete": "true"},
                    {"resource_id": "GENIE", "delta_dbus": "0.326", "delta_usd": None, "usd_complete": "false"}]
        if "billing_origin_product, ROUND" in q:
            return [{"billing_origin_product": "GENIE", "dbus": "0.326", "list_cost_usd": None, "usd_complete": "false"}]
        return []
    sp = call(build(r6), "explain_cost_spike", date="2026-10-26")
    assert sp["usd_complete"] is False and sp["unpriced_resources"] == ["GENIE"]
    assert sp["top_movers"][1]["usd_complete"] is False
    cs = call(build(r6), "cost_summary")
    assert cs["by_product"][0]["usd_complete"] is False



@pytest.mark.parametrize("obs,proj,low,withheld", [
    (0.23, None, True, "need >=3 days, have 0.23"), (2.99, None, True, "need >=3 days, have 2.99"),
    (3.0, 3.0, True, None), (6.99, 1.29, True, None), (7.0, 1.29, False, None),
    (0.0, None, True, "need >=3 days, have 0"), (None, None, True, "need >=3 days, have unknown")])
def test_projection_from_observed_coverage(obs, proj, low, withheld):
    def r7(q):
        if "AS observation_days" in q:
            return [{"observation_days": obs, "first_record": None}]
        if "est_idle_dbus" in q:
            return [{"warehouse_id": "w", "est_idle_dbus": 0.43, "est_idle_cost_usd": 0.3}]
        return []
    r = call(build(r7), "find_waste", days=30)
    assert r["measured_saving_usd"] == 0.3 and r["measured_waste_dbu"] == 0.43
    assert r["observation_days"] == obs and r["projected_30d_usd"] == proj and r["low_confidence"] is low
    assert r.get("projection_withheld") == withheld
    if withheld:
        assert r["projected_30d_dbu"] is None
    assert any("low_confidence" in n for n in r.get("notes", [])) is low


def test_no_access_note_says_unknown():
    def deny(q):
        raise RuntimeError("[INSUFFICIENT_PERMISSIONS] User does not have SELECT on Table 'system.billing.usage'.")
    r = call(build(deny), "find_waste")
    note = [n for n in r["notes"] if "low_confidence" in n][0]
    assert "unknown" in note and "0 days" not in note and r["projection_withheld"] == "need >=3 days, have unknown"


def test_projection_with_missing_price():
    def r8(q):
        if "AS observation_days" in q:
            return [{"observation_days": 10.0}]
        if "est_idle_dbus" in q:
            return [{"warehouse_id": "a", "est_idle_dbus": 2.0, "est_idle_cost_usd": 1.4},
                    {"warehouse_id": "b", "est_idle_dbus": 3.0, "est_idle_cost_usd": None}]
        return []
    r = call(build(r8), "find_waste")
    assert r["measured_saving_usd"] == 1.4 and r["projected_30d_usd"] == 4.2 and r["usd_complete"] is False
    assert r["projected_30d_dbu"] == 15.0 and r["low_confidence"] is False
