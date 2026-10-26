"""Local test: parse notebook SQL as Spark SQL (sqlglot), transpile to DuckDB, run on mock system tables."""
import runpy, pathlib
sqlglot = __import__("pytest").importorskip("sqlglot")
duckdb = __import__("pytest").importorskip("duckdb")


NB = str(pathlib.Path(__file__).parents[1] / "notebook/dbu_waste_finder.py")


def make_db(empty_prices=False):
    con = duckdb.connect()
    con.execute("""
    CREATE TABLE billing_usage AS SELECT * FROM (VALUES
     ('w1','PREMIUM_ALL_PURPOSE_COMPUTE','AWS','DBU',100.0, current_date-1, current_timestamp-INTERVAL 1 DAY,'ALL_PURPOSE', {'cluster_id':'c_ap','job_id':NULL,'warehouse_id':NULL}),
     ('w1','PREMIUM_ALL_PURPOSE_COMPUTE','AWS','DBU',50.0, current_date-2, current_timestamp-INTERVAL 2 DAY,'ALL_PURPOSE', {'cluster_id':'c_idle','job_id':NULL,'warehouse_id':NULL}),
     ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',200.0, current_date-3, current_timestamp-INTERVAL 3 DAY,'JOBS', {'cluster_id':'c_job','job_id':'j2','warehouse_id':NULL}),
     ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',999.0, current_date-90, current_timestamp-INTERVAL 90 DAY,'JOBS', {'cluster_id':'c_old','job_id':'j9','warehouse_id':NULL}),
     ('w1','PREMIUM_JOBS_SERVERLESS_COMPUTE_US_EAST','AWS','DBU',4.0, current_date-1, current_timestamp-INTERVAL 1 DAY,'JOBS', {'cluster_id':NULL,'job_id':'j7','warehouse_id':NULL}),
     ('w1','PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST','AWS','DBU',4.0, current_date-1, current_timestamp-INTERVAL 1 DAY,'SQL', {'cluster_id':NULL,'job_id':NULL,'warehouse_id':'wh1'})
    ) t(workspace_id,sku_name,cloud,usage_unit,usage_quantity,usage_date,usage_end_time,billing_origin_product,usage_metadata)""")
    con.execute("""
    CREATE TABLE billing_list_prices AS SELECT * FROM (VALUES
     ('PREMIUM_ALL_PURPOSE_COMPUTE','AWS','DBU', TIMESTAMP '2020-01-01', NULL::TIMESTAMP, {'default':'0.55','effective_list':{'default':'0.55'}}),
     ('PREMIUM_JOBS_COMPUTE','AWS','DBU', TIMESTAMP '2020-01-01', NULL::TIMESTAMP, {'default':'0.15','effective_list':{'default':'0.15'}}),
     ('PREMIUM_JOBS_SERVERLESS_COMPUTE_US_EAST','AWS','DBU', TIMESTAMP '2020-01-01', NULL::TIMESTAMP, {'default':'0.5','effective_list':{'default':'0.5'}}),
     ('PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST','AWS','DBU', TIMESTAMP '2020-01-01', NULL::TIMESTAMP, {'default':'0.7','effective_list':{'default':'0.7'}})
    ) t(sku_name,cloud,usage_unit,price_start_time,price_end_time,pricing)""")
    if empty_prices:
        con.execute("DELETE FROM billing_list_prices")
    # base = a whole hour ~1 day ago; wh1: up 60 min (RUNNING +1m .. STOPPED +61m), one 10 s query -> 59 idle min
    con.execute("""
    CREATE TABLE compute_warehouses AS SELECT * FROM (VALUES
     ('w1','wh1','bi-idle','PRO','2X_SMALL',10, current_timestamp-INTERVAL 3 DAY, NULL::TIMESTAMP),
     ('w1','wh1','bi-idle','PRO','2X_SMALL',90, current_timestamp-INTERVAL 2 DAY, NULL::TIMESTAMP),
     ('w1','wh2','busy','PRO','SMALL',10, current_timestamp-INTERVAL 2 DAY, NULL::TIMESTAMP),
     ('w1','wh3','gone','PRO','SMALL',0, current_timestamp-INTERVAL 2 DAY, (current_timestamp-INTERVAL 1 DAY)::TIMESTAMP),
     ('w1','wh4','never-stops','PRO','X_SMALL',0, current_timestamp-INTERVAL 2 DAY, NULL::TIMESTAMP),
     ('w1','wh5','old-gone','PRO','SMALL',0, current_timestamp-INTERVAL 99 DAY, (current_timestamp-INTERVAL 90 DAY)::TIMESTAMP)
    ) t(workspace_id,warehouse_id,warehouse_name,warehouse_type,warehouse_size,auto_stop_minutes,change_time,delete_time)""")
    con.execute("""
    CREATE TABLE compute_warehouse_events AS
    WITH b AS (SELECT date_trunc('hour', current_timestamp - INTERVAL 1 DAY)::TIMESTAMP AS t)
    SELECT * FROM (
      SELECT 'wh1', 'STARTING', 0, t FROM b UNION ALL
      SELECT 'wh1', 'RUNNING', 1, t + INTERVAL 1 MINUTE FROM b UNION ALL
      SELECT 'wh1', 'STOPPED', 0, t + INTERVAL 61 MINUTE FROM b UNION ALL
      SELECT 'wh2', 'RUNNING', 1, t FROM b UNION ALL
      SELECT 'wh2', 'STOPPED', 0, t + INTERVAL 20 MINUTE FROM b
    ) x(warehouse_id, event_type, cluster_count, event_time)""")
    con.execute("""
    CREATE TABLE query_history AS
    WITH b AS (SELECT date_trunc('hour', current_timestamp - INTERVAL 1 DAY)::TIMESTAMP AS t)
    SELECT * FROM (
      SELECT 's1', {'type':'WAREHOUSE','cluster_id':NULL,'warehouse_id':'wh1'}, t + INTERVAL 5 MINUTE + INTERVAL 10 SECOND,
             t + INTERVAL 5 MINUTE + INTERVAL 20 SECOND FROM b UNION ALL
      SELECT 's2', {'type':'WAREHOUSE','cluster_id':NULL,'warehouse_id':'wh2'}, t, t + INTERVAL 19 MINUTE FROM b
    ) x(statement_id, compute, start_time, end_time)""")
    con.execute("""
    CREATE TABLE lakeflow_job_run_timeline AS SELECT * FROM (VALUES
     ('w1','j7','r1', current_timestamp-INTERVAL 1 DAY, 'SUCCEEDED'),
     ('w1','j7','r2', current_timestamp-INTERVAL 1 DAY, 'SUCCEEDED'),
     ('w1','j7','r3', current_timestamp-INTERVAL 1 DAY, 'FAILED'),
     ('w1','j7','r4', current_timestamp-INTERVAL 1 DAY, 'SUCCEEDED')
    ) t(workspace_id,job_id,run_id,period_start_time,result_state)""")
    con.execute("""
    CREATE TABLE compute_clusters AS SELECT * FROM (VALUES
     ('w1','c_ap','shared-ap','a@x','UI', 0, current_timestamp-INTERVAL 10 DAY, NULL::TIMESTAMP),
     ('w1','c_ap','shared-ap','a@x','UI', 30, current_timestamp-INTERVAL 20 DAY, NULL::TIMESTAMP),
     ('w1','c_idle','big-idle','b@x','UI', 30, current_timestamp-INTERVAL 5 DAY, NULL::TIMESTAMP),
     ('w1','c_job','job-cl','svc','JOB', NULL, current_timestamp-INTERVAL 5 DAY, NULL::TIMESTAMP)
    ) t(workspace_id,cluster_id,cluster_name,owned_by,cluster_source,auto_termination_minutes,change_time,delete_time)""")
    con.execute("""
    CREATE TABLE lakeflow_job_task_run_timeline AS SELECT * FROM (VALUES
     ('w1','j1', current_timestamp-INTERVAL 1 DAY, ['c_ap']),
     ('w1','j2', current_timestamp-INTERVAL 3 DAY, ['c_job'])
    ) t(workspace_id,job_id,period_start_time,compute_ids)""")
    con.execute("""
    CREATE TABLE compute_node_timeline AS SELECT * FROM (VALUES
     ('c_idle', current_timestamp-INTERVAL 2 DAY, 3.0, 1.0, 12.0),
     ('c_ap', current_timestamp-INTERVAL 1 DAY, 60.0, 10.0, 70.0)
    ) t(cluster_id,start_time,cpu_user_percent,cpu_system_percent,mem_used_percent)""")
    MAP = {"system.billing.usage":"billing_usage","system.billing.list_prices":"billing_list_prices",
           "system.compute.clusters":"compute_clusters","system.compute.node_timeline":"compute_node_timeline",
           "system.lakeflow.job_task_run_timeline":"lakeflow_job_task_run_timeline",
           "system.lakeflow.job_run_timeline":"lakeflow_job_run_timeline",
           "system.compute.warehouses":"compute_warehouses", "system.compute.warehouse_events":"compute_warehouse_events",
           "system.query.history":"query_history"}
    def run(q):
        sqlglot.parse_one(q, read="databricks")  # raises on Spark/Databricks syntax error
        for k, v in MAP.items(): q = q.replace(k, v)
        d = sqlglot.transpile(q, read="databricks", write="duckdb")[0]
        cur = con.execute(d); cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    return run


def _f(x):
    return None if x is None else float(x)


def test_notebook_queries_on_mock_system_tables():
    nb = runpy.run_path(NB)
    res = nb["run_all"](make_db())
    for k, v in res.items(): print(k, v)
    assert set(res) == set(nb["QUERIES"]), "a query failed"
    cbp = res["cost_by_product"][0]  # 150*0.55 ; 90-day row excluded
    assert cbp["billing_origin_product"] == "ALL_PURPOSE" and cbp["list_cost_usd"] == 82.5 and _f(cbp["dbus"]) == 150
    assert {r["cluster_id"] for r in res["missing_autotermination"]} == {"c_ap"}   # latest row has 0; JOB cluster ignored
    ap = res["jobs_on_all_purpose"]; assert len(ap) == 1 and ap[0]["cluster_id"] == "c_ap"
    assert abs(ap[0]["est_saving_if_job_compute_usd"] - round(55*(1-0.15/0.55), 2)) < 0.01  # 40.0
    idle = res["idle_or_oversized"]; assert [r["cluster_id"] for r in idle] == ["c_idle"] and idle[0]["est_saving_usd"] == 13.75
    # serverless
    # 90 > 60, 0 = never; wh3 deleted 1 day ago = inside window -> included (it burned DBUs before deletion);
    # wh5 deleted 90 days ago -> outside window
    lng = {r["warehouse_id"]: r for r in res["warehouse_long_autostop"]}
    assert set(lng) == {"wh1", "wh3", "wh4"}
    assert lng["wh3"]["delete_time"] is not None and lng["wh1"]["delete_time"] is None
    idle = res["idle_warehouse"]; assert [r["warehouse_id"] for r in idle] == ["wh1"]  # wh2 busy + <30 min up
    assert _f(idle[0]["uptime_min"]) == 60.0 and idle[0]["busy_min"] == 1 and _f(idle[0]["est_idle_dbus"]) == 3.93
    # priced with the warehouse's own billed SKU (PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST, $0.70/DBU)
    assert idle[0]["sku_name"] == "PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST" and _f(idle[0]["usd_per_dbu"]) == 0.7
    assert _f(idle[0]["est_idle_cost_usd"]) == 2.75
    sj = res["serverless_jobs"]; assert len(sj) == 1 and sj[0]["job_id"] == "j7"
    assert (sj[0]["runs"], sj[0]["failed_runs"]) == (4, 1) and _f(sj[0]["dbus"]) == 4.0
    assert _f(sj[0]["est_waste_dbus"]) == 1.0 and _f(sj[0]["est_saving_usd"]) == 0.5  # 1/4 of 4 DBU x $0.5
    assert nb["summarize"](res) == 57.0           # 40 + 13.75 + 0.5 + 2.75 idle warehouse
    assert nb["summarize_dbus"](res) == 4.93      # 3.93 idle wh + 1.0 failed run
    print("ALL TESTS PASSED")


def test_empty_list_prices_gives_unknown_usd_not_zero():
    nb = runpy.run_path(NB)
    res = nb["run_all"](make_db(empty_prices=True))
    assert set(res) == set(nb["QUERIES"]), "a query failed"
    assert res["cost_by_product"] and all(r["list_cost_usd"] is None for r in res["cost_by_product"])
    assert {r["resource_id"] for r in res["top_cost_drivers"]} >= {"wh1", "j7"}
    assert all(r["list_cost_usd"] is None and _f(r["dbus"]) > 0 for r in res["top_cost_drivers"])
    assert res["serverless_jobs"][0]["est_saving_usd"] is None and _f(res["serverless_jobs"][0]["est_waste_dbus"]) == 1.0
    assert nb["summarize"](res) is None            # unknown, not $0
    assert res["idle_warehouse"][0]["est_idle_cost_usd"] is None
    assert nb["summarize_dbus"](res) == 4.93


def test_lookback_zero_no_division_by_zero(tmp_path):
    src = pathlib.Path(NB).read_text().replace("LOOKBACK_DAYS = 30", "LOOKBACK_DAYS = 0", 1)
    f = tmp_path / "nb0.py"; f.write_text(src)
    nb = runpy.run_path(str(f))                     # must not raise ZeroDivisionError at import
    import pytest
    with pytest.raises(ValueError, match="LOOKBACK_DAYS"):
        nb["render"]("cost_by_product")
    assert nb["summarize"]({}) == 0.0               # no ZeroDivisionError
    assert nb["summarize_dbus"]({}) == 0.0
