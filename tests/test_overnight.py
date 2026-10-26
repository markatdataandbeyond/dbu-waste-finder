"""Overnight 2026-10-27: notebook <-> MCP parity on the same mock window, notebook Databricks path end to end,
and robustness edge cases (empty billing, missing grants, currency_code, 90-day window, spike date boundaries)."""
import asyncio, json, pathlib, runpy, sys
import pytest
from test_notebook import make_db, NB, _f

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "mcp/src"))
from dbu_mcp.server import build  # noqa: E402

EMPTY = [f"DELETE FROM {t}" for t in ("billing_usage", "compute_warehouse_events", "query_history",
                                      "lakeflow_job_run_timeline", "lakeflow_job_task_run_timeline",
                                      "compute_node_timeline", "compute_clusters", "compute_warehouses")]


def call(srv, name, **args):
    res = asyncio.run(srv.call_tool(name, args))
    assert not res.is_error, res.content
    sc = res.structured_content
    return sc.get("result", sc) if isinstance(sc, dict) else json.loads(res.content[0].text)


def nb_with(tmp_path, days):
    src = pathlib.Path(NB).read_text().replace("LOOKBACK_DAYS = 30", f"LOOKBACK_DAYS = {days}", 1)
    f = tmp_path / f"nb{days}.py"; f.write_text(src)
    return runpy.run_path(str(f))


def _norm(rows):  # Decimal may serialize as '4.0' over MCP; compare values, not types
    def v(x):
        try: return float(x) if isinstance(x, str) and x.replace('.', '', 1).isdigit() else x
        except ValueError: return x
    return [{k: v(x) for k, x in r.items()} for r in rows]


# ---------- 1. notebook and MCP give the same numbers for the same window ----------
@pytest.mark.parametrize("days", [30, 90])
def test_notebook_and_mcp_agree(days, tmp_path):
    run = make_db()
    nb = nb_with(tmp_path, days)
    res = nb["run_all"](run); sm = nb["summarize"](res)
    srv = build(run)
    fw = call(srv, "find_waste", days=days)
    assert fw["measured_saving_usd"] == sm["measured_saving_usd"]
    assert fw["measured_waste_dbu"] == sm["measured_waste_dbu"]
    assert fw["observation_days"] == sm["observation_days"]
    assert fw["projected_30d_usd"] == sm["projected_30d_usd"]
    for k in ("idle_warehouse", "serverless_jobs", "warehouse_long_autostop", "jobs_on_all_purpose"):
        assert _norm(fw[k]) == _norm(json.loads(json.dumps(res[k], default=lambda x: x.isoformat() if hasattr(x, "isoformat") else float(x)))), k
    cs = call(srv, "cost_summary", days=days)
    nb_total = round(sum(float(r["list_cost_usd"]) for r in res["cost_by_product"] if r["list_cost_usd"] is not None), 2)
    assert cs["total_list_cost_usd"] == nb_total
    assert [r["sku_name"] for r in cs["unpriced_skus"]] == nb["unpriced"](res)["skus"]
    td = call(srv, "top_cost_drivers", days=days)
    assert [r["resource_id"] for r in td["rows"]] == [r["resource_id"] for r in res["top_cost_drivers"]]


def test_90_day_window_includes_day_90_row(tmp_path):
    run = make_db()
    cs = call(build(run), "cost_summary", days=90)
    jobs = [r for r in cs["by_product"] if r["billing_origin_product"] == "JOBS"][0]
    assert _f(jobs["dbus"]) == 200 + 999 + 4           # the 90-day-old 999 DBU row is inside a 90-day window
    assert call(build(run), "find_waste", days=90)["observation_days"] <= 90


# ---------- 2. notebook Databricks branch (spark + display) end to end ----------
class _Row(dict):
    def asDict(self): return dict(self)


class _DF(list):
    def collect(self): return [_Row(r) for r in self]


def test_notebook_databricks_path_prints_same_totals_as_mcp(capsys):
    run = make_db()
    shown = []
    g = runpy.run_path(NB, init_globals={"spark": type("S", (), {"sql": staticmethod(lambda q: _DF(run(q)))})(),
                                         "display": shown.append})
    out = capsys.readouterr().out
    fw = call(build(make_db()), "find_waste", days=30)
    assert "[skip]" not in out and len(shown) == len(g["QUERIES"])
    assert f"measured ${fw['measured_saving_usd']:,.2f}" in out
    assert f"projected 30 days: {fw['projected_30d_usd']}" in out
    assert f"measured {fw['measured_waste_dbu']}" in out
    assert "INCOMPLETE" in out and "GENIE_FREE_USAGE" in out
    assert "Warehouses with long/no auto-stop: 3" in out


# ---------- 3a. empty billing (brand-new workspace) ----------
def test_empty_billing_new_workspace():
    run = make_db(extra=EMPTY)
    srv = build(run)
    cs = call(srv, "cost_summary", days=30)
    assert cs["total_list_cost_usd"] == 0.0 and cs["by_product"] == [] and cs["usd_complete"] is True
    assert "errors" not in cs
    fw = call(srv, "find_waste", days=30)
    assert "errors" not in fw and fw["measured_saving_usd"] == 0.0 and fw["projected_30d_usd"] is None
    note = " ".join(fw["notes"])
    assert "could not be read" not in note            # the data WAS read; there just isn't any yet
    assert "no usage records" in note
    assert call(srv, "top_cost_drivers", days=30)["rows"] == []
    sp = call(srv, "explain_cost_spike", date="2026-10-26")
    assert sp["top_movers"] == [] and sp["net_delta_top15_usd"] == 0.0


def test_empty_billing_notebook_note(tmp_path):
    nb = runpy.run_path(NB)
    res = nb["run_all"](make_db(extra=EMPTY))
    sm = nb["summarize"](res)
    assert sm["observation_days"] is None and "no usage records" in sm["note"]


# ---------- 3b. missing grants -> clear message ----------
DENIED = ("[INSUFFICIENT_PERMISSIONS] Insufficient privileges: User does not have USE SCHEMA on Schema "
          "'system.billing'. SQLSTATE: 42501")


def _deny_billing(q):
    if "system.billing" in q:
        raise RuntimeError(DENIED)
    return []


def test_permission_error_mcp_names_grant():
    srv = build(_deny_billing)
    cs = call(srv, "cost_summary", days=30)
    e = cs["errors"]["cost_summary"]
    assert cs["total_list_cost_usd"] is None and e["reason"] == "permission_denied"
    assert "GRANT SELECT ON TABLE system.billing.usage" in e["fix"] and "USE SCHEMA" in e["fix"]
    fw = call(srv, "find_waste", days=30)
    assert fw["errors"]["serverless_jobs"]["reason"] == "permission_denied"
    assert "observation_days unknown" in " ".join(fw["notes"])


def test_permission_error_notebook_gives_fix(capsys):
    nb = runpy.run_path(NB)
    nb["run_all"](_deny_billing)
    out = capsys.readouterr().out
    assert "[skip] cost_by_product" in out
    assert "GRANT SELECT ON TABLE system.billing.usage" in out   # actionable, not just the raw error


# ---------- 3c. currency_code variations in list_prices ----------
NON_USD = ["""INSERT INTO billing_list_prices SELECT sku_name, cloud, usage_unit, price_start_time, price_end_time,
           {'default':'9.99','effective_list':{'default':'9.99'}}, 'EUR' FROM billing_list_prices"""]


def test_non_usd_price_rows_do_not_double_count():
    base = call(build(make_db()), "cost_summary", days=30)
    cs = call(build(make_db(extra=NON_USD)), "cost_summary", days=30)
    assert cs["total_dbus"] == base["total_dbus"]                       # no fan-out from the extra currency
    assert cs["total_list_cost_usd"] == base["total_list_cost_usd"]     # EUR price never used as USD
    fw = call(build(make_db(extra=NON_USD)), "find_waste", days=30)
    assert fw["idle_warehouse"][0]["usd_per_dbu"] == 0.7
    sp = call(build(make_db(extra=NON_USD)), "explain_cost_spike", date=str(__import__("datetime").date.today()))
    sp0 = call(build(make_db()), "explain_cost_spike", date=str(__import__("datetime").date.today()))
    assert sp["net_delta_top15_usd"] == sp0["net_delta_top15_usd"]


def test_only_non_usd_prices_means_unknown_not_converted():
    sql = ["UPDATE billing_list_prices SET currency_code = 'EUR'"]
    cs = call(build(make_db(extra=sql)), "cost_summary", days=30)
    assert cs["usd_complete"] is False and all(r["list_cost_usd"] is None for r in cs["by_product"])


# ---------- 3d. explain_cost_spike day boundaries (UTC usage_date) ----------
SPIKE_ROWS = ["DELETE FROM billing_usage", """INSERT INTO billing_usage (workspace_id, sku_name, cloud, usage_unit,
  usage_quantity, usage_date, usage_end_time, billing_origin_product, usage_metadata, usage_start_time) VALUES
  ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',30.0, DATE '2026-10-26', TIMESTAMP '2026-10-26 23:59:59','JOBS',
   {'cluster_id':NULL,'job_id':'late','warehouse_id':NULL}, TIMESTAMP '2026-10-26 23:00:00'),
  ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',20.0, DATE '2026-10-27', TIMESTAMP '2026-10-27 00:30:00','JOBS',
   {'cluster_id':NULL,'job_id':'next','warehouse_id':NULL}, TIMESTAMP '2026-10-27 00:00:00'),
  ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',70.0, DATE '2026-10-19', TIMESTAMP '2026-10-19 12:00:00','JOBS',
   {'cluster_id':NULL,'job_id':'late','warehouse_id':NULL}, TIMESTAMP '2026-10-19 11:00:00'),
  ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',500.0, DATE '2026-10-18', TIMESTAMP '2026-10-18 12:00:00','JOBS',
   {'cluster_id':NULL,'job_id':'late','warehouse_id':NULL}, TIMESTAMP '2026-10-18 11:00:00')"""]


def test_spike_uses_utc_usage_date_boundaries():
    sp = call(build(make_db(extra=SPIKE_ROWS)), "explain_cost_spike", date="2026-10-26")
    rows = {r["resource_id"]: r for r in sp["top_movers"]}
    assert set(rows) == {"late"}                       # 00:30 UTC on the 27th belongs to the 27th, not the 26th
    late = rows["late"]
    assert _f(late["day_dbus"]) == 30.0
    assert _f(late["delta_dbus"]) == 20.0              # baseline = 10-19..10-25 (70/7); 10-18 is outside the 7 days


def test_spike_docstring_and_datetime_input():
    srv = build(make_db())
    tool = [t for t in asyncio.run(srv.list_tools()) if t.name == "explain_cost_spike"][0]
    assert "UTC" in tool.description
    with pytest.raises(Exception, match=r"YYYY-MM-DD \(a UTC billing day"):
        asyncio.run(srv.call_tool("explain_cost_spike", {"date": "2026-10-26T23:30:00+01:00"}))
