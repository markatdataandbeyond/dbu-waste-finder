"""Local test: parse notebook SQL as Spark SQL (sqlglot), transpile to DuckDB, run on mock system tables."""
import runpy, sqlglot, duckdb, pathlib
nb = runpy.run_path(str(pathlib.Path(__file__).parents[1] / "notebook/dbu_waste_finder.py"))
con = duckdb.connect()
con.execute("""
CREATE TABLE billing_usage AS SELECT * FROM (VALUES
 ('w1','PREMIUM_ALL_PURPOSE_COMPUTE','AWS','DBU',100.0, current_date-1, current_timestamp-INTERVAL 1 DAY,'ALL_PURPOSE', {'cluster_id':'c_ap','job_id':NULL,'warehouse_id':NULL}),
 ('w1','PREMIUM_ALL_PURPOSE_COMPUTE','AWS','DBU',50.0, current_date-2, current_timestamp-INTERVAL 2 DAY,'ALL_PURPOSE', {'cluster_id':'c_idle','job_id':NULL,'warehouse_id':NULL}),
 ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',200.0, current_date-3, current_timestamp-INTERVAL 3 DAY,'JOBS', {'cluster_id':'c_job','job_id':'j2','warehouse_id':NULL}),
 ('w1','PREMIUM_JOBS_COMPUTE','AWS','DBU',999.0, current_date-90, current_timestamp-INTERVAL 90 DAY,'JOBS', {'cluster_id':'c_old','job_id':'j9','warehouse_id':NULL})
) t(workspace_id,sku_name,cloud,usage_unit,usage_quantity,usage_date,usage_end_time,billing_origin_product,usage_metadata)""")
con.execute("""
CREATE TABLE billing_list_prices AS SELECT * FROM (VALUES
 ('PREMIUM_ALL_PURPOSE_COMPUTE','AWS','DBU', TIMESTAMP '2020-01-01', NULL::TIMESTAMP, {'default':'0.55','effective_list':{'default':'0.55'}}),
 ('PREMIUM_JOBS_COMPUTE','AWS','DBU', TIMESTAMP '2020-01-01', NULL::TIMESTAMP, {'default':'0.15','effective_list':{'default':'0.15'}})
) t(sku_name,cloud,usage_unit,price_start_time,price_end_time,pricing)""")
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
       "system.lakeflow.job_task_run_timeline":"lakeflow_job_task_run_timeline"}
def run(q):
    sqlglot.parse_one(q, read="databricks")  # raises on Spark/Databricks syntax error
    for k, v in MAP.items(): q = q.replace(k, v)
    d = sqlglot.transpile(q, read="databricks", write="duckdb")[0]
    cur = con.execute(d); cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
res = nb["run_all"](run)
for k, v in res.items(): print(k, v)
assert set(res) == set(nb["QUERIES"]), "a query failed"
assert res["cost_by_product"][0] == {"billing_origin_product": "ALL_PURPOSE", "list_cost_usd": 82.5}  # 150*0.55 ; 90-day row excluded
assert {r["cluster_id"] for r in res["missing_autotermination"]} == {"c_ap"}   # latest row has 0; JOB cluster ignored
ap = res["jobs_on_all_purpose"]; assert len(ap) == 1 and ap[0]["cluster_id"] == "c_ap"
assert abs(ap[0]["est_saving_if_job_compute_usd"] - round(55*(1-0.15/0.55), 2)) < 0.01  # 40.0
idle = res["idle_or_oversized"]; assert [r["cluster_id"] for r in idle] == ["c_idle"] and idle[0]["est_saving_usd"] == 13.75
assert nb["summarize"](res) == 53.75
print("ALL TESTS PASSED")
