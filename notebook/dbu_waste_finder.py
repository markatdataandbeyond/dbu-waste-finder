# Databricks notebook source
# MAGIC %md
# MAGIC # DBU Waste Finder (free, MIT)
# MAGIC Read-only cost check on Databricks **system tables**. Nothing leaves your workspace.
# MAGIC
# MAGIC Checks: top cost drivers, jobs on all-purpose compute, missing/long auto-termination,
# MAGIC idle or oversized clusters, SQL warehouses with long/no auto-stop, idle SQL warehouses,
# MAGIC serverless job cost (incl. failed runs), and an estimated monthly saving.
# MAGIC
# MAGIC If `system.billing.list_prices` has no matching price, cost columns are NULL (= **unknown**, not $0)
# MAGIC and DBU columns are still shown.
# MAGIC
# MAGIC Requires Unity Catalog with the `system.billing`, `system.compute` and `system.lakeflow` schemas enabled
# MAGIC and SELECT on them. Costs are at **list price** (`pricing.effective_list.default`), not your negotiated discount.

# COMMAND ----------

LOOKBACK_DAYS = 30          # analysis window
IDLE_CPU_PCT = 10.0         # avg CPU (user+system) below this = idle/oversized
IDLE_MEM_PCT = 30.0         # avg memory used below this = idle/oversized
IDLE_SAVING_SHARE = 0.5     # ASSUMPTION: right-sizing an idle cluster saves ~50% of its cost
MAX_AUTOTERM_MIN = 60       # auto-termination / warehouse auto-stop above this is flagged
WH_IDLE_PCT = 80.0          # warehouse uptime with no query running above this % = idle
WH_MIN_UPTIME_MIN = 30      # ignore warehouses up less than this many minutes in the window

def month_factor():
    # LOOKBACK_DAYS < 1 is invalid (render() raises); never divide by zero here
    return 30.0 / LOOKBACK_DAYS if LOOKBACK_DAYS >= 1 else 0.0

# Spark SQL. {d} = lookback days. Column names verified against docs.databricks.com
# (admin/system-tables/billing, /pricing, /compute, /jobs), fetched 2026-10-26.
PRICED_USAGE = """
WITH priced AS (
  SELECT u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_date,
         u.usage_metadata.cluster_id   AS cluster_id,
         u.usage_metadata.job_id       AS job_id,
         u.usage_metadata.warehouse_id AS warehouse_id,
         u.usage_quantity AS dbus,
         u.usage_quantity * CAST(p.pricing.effective_list.default AS DOUBLE) AS list_cost
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= date_add(current_date(), -{d})
)
"""

QUERIES = {
"cost_by_product": PRICED_USAGE + """
SELECT billing_origin_product, ROUND(SUM(dbus), 3) AS dbus,
       CASE WHEN COUNT(list_cost) = COUNT(*) THEN ROUND(SUM(list_cost), 2) END AS list_cost_usd
FROM priced GROUP BY billing_origin_product HAVING SUM(dbus) <> 0
ORDER BY list_cost_usd DESC NULLS LAST, dbus DESC""",

"top_cost_drivers": PRICED_USAGE + """
SELECT CASE WHEN job_id IS NOT NULL THEN 'job'
            WHEN warehouse_id IS NOT NULL THEN 'warehouse'
            WHEN cluster_id IS NOT NULL THEN 'cluster'
            ELSE 'other' END AS resource_type,
       COALESCE(job_id, warehouse_id, cluster_id, billing_origin_product) AS resource_id,
       ROUND(SUM(dbus), 3) AS dbus,
       CASE WHEN COUNT(list_cost) = COUNT(*) THEN ROUND(SUM(list_cost), 2) END AS list_cost_usd
FROM priced GROUP BY 1, 2 HAVING SUM(dbus) <> 0
ORDER BY list_cost_usd DESC NULLS LAST, dbus DESC LIMIT 20""",

# clusters table is SCD2: take latest row per cluster
"missing_autotermination": """
WITH latest AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) AS rn
  FROM system.compute.clusters
)
SELECT workspace_id, cluster_id, cluster_name, owned_by, cluster_source, auto_termination_minutes
FROM latest
WHERE rn = 1 AND delete_time IS NULL AND cluster_source IN ('UI', 'API')
  AND (auto_termination_minutes IS NULL OR auto_termination_minutes = 0
       OR auto_termination_minutes > {maxterm})
ORDER BY auto_termination_minutes DESC NULLS FIRST""",

"jobs_on_all_purpose": PRICED_USAGE + """
, ap AS (
  SELECT cluster_id, SUM(list_cost) AS ap_cost FROM priced
  WHERE billing_origin_product = 'ALL_PURPOSE' AND cluster_id IS NOT NULL GROUP BY cluster_id
), ratio AS (
  SELECT AVG(CASE WHEN sku_name LIKE '%JOBS_COMPUTE%' AND sku_name NOT LIKE '%SERVERLESS%' AND sku_name NOT LIKE '%PHOTON%'
                  THEN CAST(pricing.effective_list.default AS DOUBLE) END)
       / AVG(CASE WHEN sku_name LIKE '%ALL_PURPOSE_COMPUTE%' AND sku_name NOT LIKE '%SERVERLESS%' AND sku_name NOT LIKE '%PHOTON%'
                  THEN CAST(pricing.effective_list.default AS DOUBLE) END) AS jobs_to_ap
  FROM system.billing.list_prices WHERE price_end_time IS NULL
), runs AS (
  SELECT DISTINCT t.workspace_id, t.job_id, c.cluster_id
  FROM system.lakeflow.job_task_run_timeline t
  JOIN (SELECT DISTINCT cluster_id FROM system.compute.clusters WHERE cluster_source IN ('UI','API')) c
    ON array_contains(t.compute_ids, c.cluster_id)
  WHERE t.period_start_time >= date_add(current_date(), -{d})
)
SELECT r.cluster_id, COUNT(DISTINCT r.job_id) AS jobs_using_cluster,
       ROUND(MAX(a.ap_cost), 2) AS cluster_all_purpose_cost_usd,
       ROUND(MAX(a.ap_cost) * (1 - MAX(x.jobs_to_ap)), 2) AS est_saving_if_job_compute_usd
FROM runs r JOIN ap a ON r.cluster_id = a.cluster_id CROSS JOIN ratio x
GROUP BY r.cluster_id ORDER BY est_saving_if_job_compute_usd DESC""",

"idle_or_oversized": PRICED_USAGE + """
, util AS (
  SELECT cluster_id, AVG(cpu_user_percent + cpu_system_percent) AS avg_cpu, AVG(mem_used_percent) AS avg_mem
  FROM system.compute.node_timeline
  WHERE start_time >= date_add(current_date(), -{d}) GROUP BY cluster_id
), cost AS (
  SELECT cluster_id, SUM(list_cost) AS c FROM priced WHERE cluster_id IS NOT NULL GROUP BY cluster_id
)
SELECT u.cluster_id, ROUND(u.avg_cpu, 1) AS avg_cpu_pct, ROUND(u.avg_mem, 1) AS avg_mem_pct,
       ROUND(c.c, 2) AS list_cost_usd, ROUND(c.c * {idle_share}, 2) AS est_saving_usd
FROM util u JOIN cost c ON u.cluster_id = c.cluster_id
WHERE u.avg_cpu < {cpu} AND u.avg_mem < {mem}
ORDER BY est_saving_usd DESC""",

# warehouses table is SCD2 too. auto_stop_minutes = 0 means never auto-stops.
# Warehouses deleted inside the window are kept (they burned DBUs before deletion); delete_time shows it.
"warehouse_long_autostop": """
WITH latest AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn
  FROM system.compute.warehouses
)
SELECT workspace_id, warehouse_id, warehouse_name, warehouse_type, warehouse_size, auto_stop_minutes, delete_time
FROM latest
WHERE rn = 1 AND (delete_time IS NULL OR delete_time >= date_add(current_date(), -{d}))
  AND (auto_stop_minutes IS NULL OR auto_stop_minutes = 0 OR auto_stop_minutes > {maxterm})
ORDER BY auto_stop_minutes DESC NULLS FIRST""",

# Uptime from warehouse_events (cluster_count x time until next event) vs. time with a query running
# (query.history, overlaps merged per minute). DBU/h per cluster by size = Databricks SQL published rates
# (ASSUMPTION; independent of billing lag and of list_prices).
"idle_warehouse": """
WITH ev AS (
  SELECT warehouse_id, event_time, cluster_count,
         LEAD(event_time) OVER (PARTITION BY warehouse_id ORDER BY event_time) AS next_time
  FROM system.compute.warehouse_events
  WHERE event_time >= date_add(current_date(), -{d})
), up AS (
  SELECT warehouse_id,
         SUM(TIMESTAMPDIFF(SECOND, event_time, COALESCE(next_time, current_timestamp())) * cluster_count)
             / 60.0 AS uptime_min
  FROM ev GROUP BY warehouse_id
), busy AS (
  SELECT warehouse_id, COUNT(DISTINCT minute_bucket) AS busy_min, COUNT(DISTINCT statement_id) AS queries
  FROM (
    SELECT compute.warehouse_id AS warehouse_id, statement_id,
           explode(sequence(TIMESTAMPDIFF(MINUTE, TIMESTAMP'1970-01-01 00:00:00', start_time),
                            TIMESTAMPDIFF(MINUTE, TIMESTAMP'1970-01-01 00:00:00', COALESCE(end_time, start_time))))
             AS minute_bucket
    FROM system.query.history
    WHERE compute.warehouse_id IS NOT NULL AND start_time >= date_add(current_date(), -{d})
  ) q GROUP BY warehouse_id
), wh AS (
  SELECT warehouse_id, warehouse_name, warehouse_size, auto_stop_minutes FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) AS rn
    FROM system.compute.warehouses) x WHERE rn = 1
), wsku AS (  -- the SKU this warehouse is actually billed under, and its current list price
  SELECT s.warehouse_id, s.sku_name, CAST(p.pricing.effective_list.default AS DOUBLE) AS usd_per_dbu FROM (
    SELECT usage_metadata.warehouse_id AS warehouse_id, sku_name, cloud, usage_unit,
           ROW_NUMBER() OVER (PARTITION BY usage_metadata.warehouse_id ORDER BY usage_end_time DESC) AS rn
    FROM system.billing.usage
    WHERE usage_metadata.warehouse_id IS NOT NULL AND usage_date >= date_add(current_date(), -{d})
  ) s LEFT JOIN system.billing.list_prices p
    ON s.sku_name = p.sku_name AND s.cloud = p.cloud AND s.usage_unit = p.usage_unit AND p.price_end_time IS NULL
  WHERE s.rn = 1
), res AS (
SELECT u.warehouse_id, w.warehouse_name, w.warehouse_size, w.auto_stop_minutes,
       ROUND(u.uptime_min, 1) AS uptime_min, COALESCE(b.busy_min, 0) AS busy_min, COALESCE(b.queries, 0) AS queries,
       ROUND(100.0 * GREATEST(u.uptime_min - COALESCE(b.busy_min, 0), 0) / u.uptime_min, 1) AS idle_pct,
       ROUND(GREATEST(u.uptime_min - COALESCE(b.busy_min, 0), 0) / 60.0
             * CASE w.warehouse_size WHEN '2X_SMALL' THEN 4 WHEN 'X_SMALL' THEN 6 WHEN 'SMALL' THEN 12
                   WHEN 'MEDIUM' THEN 24 WHEN 'LARGE' THEN 40 WHEN 'X_LARGE' THEN 80 WHEN '2X_LARGE' THEN 144
                   WHEN '3X_LARGE' THEN 272 WHEN '4X_LARGE' THEN 528 END, 2) AS est_idle_dbus,
       k.sku_name, k.usd_per_dbu
FROM up u LEFT JOIN busy b ON u.warehouse_id = b.warehouse_id LEFT JOIN wh w ON u.warehouse_id = w.warehouse_id
LEFT JOIN wsku k ON u.warehouse_id = k.warehouse_id
WHERE u.uptime_min >= {min_up}
  AND 100.0 * GREATEST(u.uptime_min - COALESCE(b.busy_min, 0), 0) / u.uptime_min >= {idle_pct}
)
SELECT *, ROUND(est_idle_dbus * usd_per_dbu, 2) AS est_idle_cost_usd  -- NULL = unknown (no billed SKU/price yet)
FROM res ORDER BY est_idle_dbus DESC NULLS LAST""",

# Serverless jobs: DBUs/cost per job and share spent on failed runs (est. waste = failed-run share of cost).
"serverless_jobs": PRICED_USAGE + """
, sj AS (
  SELECT job_id, SUM(dbus) AS dbus,
         CASE WHEN COUNT(list_cost) = COUNT(*) THEN SUM(list_cost) END AS cost
  FROM priced WHERE job_id IS NOT NULL AND sku_name LIKE '%SERVERLESS%' GROUP BY job_id
), runs AS (
  SELECT job_id, COUNT(DISTINCT run_id) AS runs,
         COUNT(DISTINCT CASE WHEN result_state IN ('FAILED', 'ERROR', 'TIMED_OUT') THEN run_id END) AS failed_runs
  FROM system.lakeflow.job_run_timeline
  WHERE period_start_time >= date_add(current_date(), -{d}) GROUP BY job_id
)
SELECT s.job_id, ROUND(s.dbus, 3) AS dbus, ROUND(s.cost, 2) AS list_cost_usd,
       COALESCE(r.runs, 0) AS runs, COALESCE(r.failed_runs, 0) AS failed_runs,
       ROUND(s.dbus / NULLIF(r.runs, 0), 4) AS dbus_per_run,
       ROUND(s.dbus * COALESCE(r.failed_runs, 0) / NULLIF(r.runs, 0), 3) AS est_waste_dbus,
       ROUND(s.cost * COALESCE(r.failed_runs, 0) / NULLIF(r.runs, 0), 2) AS est_saving_usd
FROM sj s LEFT JOIN runs r ON s.job_id = r.job_id
ORDER BY dbus DESC""",
}

def render(name):
    if not 1 <= int(LOOKBACK_DAYS) <= 365:
        raise ValueError(f"LOOKBACK_DAYS must be 1..365, got {LOOKBACK_DAYS}")
    return QUERIES[name].format(d=int(LOOKBACK_DAYS), maxterm=int(MAX_AUTOTERM_MIN),
                                idle_share=IDLE_SAVING_SHARE, cpu=IDLE_CPU_PCT, mem=IDLE_MEM_PCT,
                                idle_pct=float(WH_IDLE_PCT), min_up=int(WH_MIN_UPTIME_MIN))

# COMMAND ----------

def run_all(sql_fn):
    out = {}
    for name in QUERIES:
        try:
            out[name] = sql_fn(render(name))
        except Exception as e:  # e.g. a system schema not enabled
            print(f"[skip] {name}: {str(e)[:200]}")
    return out

SAVING_COLS = (("jobs_on_all_purpose", "est_saving_if_job_compute_usd"), ("idle_or_oversized", "est_saving_usd"),
               ("serverless_jobs", "est_saving_usd"), ("idle_warehouse", "est_idle_cost_usd"))
WASTE_DBU_COLS = (("idle_warehouse", "est_idle_dbus"), ("serverless_jobs", "est_waste_dbus"))


def summarize(rows_by_check):
    """rows_by_check: name -> list of dict rows. Returns monthly saving estimate (USD, list price),
    or None if a flagged row has unknown price (NULL) -- never silently $0."""
    s, unknown = 0.0, False
    for name, col in SAVING_COLS:
        for r in rows_by_check.get(name, []):
            if r.get(col) is not None:
                s += float(r[col])
            elif name != "serverless_jobs" or r.get("failed_runs"):
                unknown = True  # flagged waste whose price is missing
    return None if unknown else round(s * month_factor(), 2)


def summarize_dbus(rows_by_check):
    """Monthly DBUs flagged as waste by the DBU-based checks (works without list_prices)."""
    s = sum(float(r.get(col) or 0) for name, col in WASTE_DBU_COLS for r in rows_by_check.get(name, []))
    return round(s * month_factor(), 2)

# COMMAND ----------

if "spark" in globals():
    results = run_all(lambda q: spark.sql(q))
    for name, df in results.items():
        print(f"== {name}")
        display(df)  # noqa: F821
    rows = {k: [r.asDict() for r in v.collect()] for k, v in results.items()}
    usd = summarize(rows)
    usd_txt = f"~${usd:,.0f}/month" if usd is not None else "unknown $ (list_prices has no price for some SKUs)"
    print(f"Estimated saving at list price: {usd_txt} "
          f"(overlap between checks possible; idle saving share is an assumption: {IDLE_SAVING_SHARE:.0%}).")
    print(f"Waste in DBUs (idle warehouses + failed serverless runs): ~{summarize_dbus(rows):,.1f} DBU/month")
    print(f"Clusters missing/long auto-termination: {len(rows.get('missing_autotermination', []))}")
    print(f"Warehouses with long/no auto-stop: {len(rows.get('warehouse_long_autostop', []))}")

# COMMAND ----------

# MAGIC %md
# MAGIC Want this weekly, with alerts and owner-level breakdowns? See https://dataandbeyond.dk (Pro, waitlist).
