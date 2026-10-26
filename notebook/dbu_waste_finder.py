# Databricks notebook source
# MAGIC %md
# MAGIC # DBU Waste Finder (free, MIT)
# MAGIC Read-only cost check on Databricks **system tables**. Nothing leaves your workspace.
# MAGIC
# MAGIC Checks: top cost drivers, jobs on all-purpose compute, missing/long auto-termination,
# MAGIC idle or oversized clusters, and an estimated monthly saving.
# MAGIC
# MAGIC Requires Unity Catalog with the `system.billing`, `system.compute` and `system.lakeflow` schemas enabled
# MAGIC and SELECT on them. Costs are at **list price** (`pricing.effective_list.default`), not your negotiated discount.

# COMMAND ----------

LOOKBACK_DAYS = 30          # analysis window
IDLE_CPU_PCT = 10.0         # avg CPU (user+system) below this = idle/oversized
IDLE_MEM_PCT = 30.0         # avg memory used below this = idle/oversized
IDLE_SAVING_SHARE = 0.5     # ASSUMPTION: right-sizing an idle cluster saves ~50% of its cost
MAX_AUTOTERM_MIN = 60       # auto-termination above this is flagged
MONTH_FACTOR = 30.0 / LOOKBACK_DAYS

# Spark SQL. {d} = lookback days. Column names verified against docs.databricks.com
# (admin/system-tables/billing, /pricing, /compute, /jobs), fetched 2026-10-26.
PRICED_USAGE = """
WITH priced AS (
  SELECT u.workspace_id, u.sku_name, u.billing_origin_product, u.usage_date,
         u.usage_metadata.cluster_id   AS cluster_id,
         u.usage_metadata.job_id       AS job_id,
         u.usage_metadata.warehouse_id AS warehouse_id,
         u.usage_quantity * CAST(p.pricing.effective_list.default AS DOUBLE) AS list_cost
  FROM system.billing.usage u
  JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= date_add(current_date(), -{d})
)
"""

QUERIES = {
"cost_by_product": PRICED_USAGE + """
SELECT billing_origin_product, ROUND(SUM(list_cost), 2) AS list_cost_usd
FROM priced GROUP BY billing_origin_product HAVING SUM(list_cost) <> 0
ORDER BY list_cost_usd DESC""",

"top_cost_drivers": PRICED_USAGE + """
SELECT CASE WHEN job_id IS NOT NULL THEN 'job'
            WHEN warehouse_id IS NOT NULL THEN 'warehouse'
            WHEN cluster_id IS NOT NULL THEN 'cluster'
            ELSE 'other' END AS resource_type,
       COALESCE(job_id, warehouse_id, cluster_id, billing_origin_product) AS resource_id,
       ROUND(SUM(list_cost), 2) AS list_cost_usd
FROM priced GROUP BY 1, 2 HAVING SUM(list_cost) <> 0
ORDER BY list_cost_usd DESC LIMIT 20""",

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
}

def render(name):
    return QUERIES[name].format(d=int(LOOKBACK_DAYS), maxterm=int(MAX_AUTOTERM_MIN),
                                idle_share=IDLE_SAVING_SHARE, cpu=IDLE_CPU_PCT, mem=IDLE_MEM_PCT)

# COMMAND ----------

def run_all(sql_fn):
    out = {}
    for name in QUERIES:
        try:
            out[name] = sql_fn(render(name))
        except Exception as e:  # e.g. a system schema not enabled
            print(f"[skip] {name}: {str(e)[:200]}")
    return out

def summarize(rows_by_check):
    """rows_by_check: name -> list of dict rows. Returns monthly saving estimate (USD, list price)."""
    s = 0.0
    for name, col in (("jobs_on_all_purpose", "est_saving_if_job_compute_usd"), ("idle_or_oversized", "est_saving_usd")):
        s += sum(float(r[col] or 0) for r in rows_by_check.get(name, []))
    return round(s * MONTH_FACTOR, 2)

# COMMAND ----------

if "spark" in globals():
    results = run_all(lambda q: spark.sql(q))
    for name, df in results.items():
        print(f"== {name}")
        display(df)  # noqa: F821
    rows = {k: [r.asDict() for r in v.collect()] for k, v in results.items()}
    print(f"Estimated saving at list price: ~${summarize(rows):,.0f}/month "
          f"(overlap between checks possible; idle saving share is an assumption: {IDLE_SAVING_SHARE:.0%}).")
    print(f"Clusters missing/long auto-termination: {len(rows.get('missing_autotermination', []))}")

# COMMAND ----------

# MAGIC %md
# MAGIC Want this weekly, with alerts and owner-level breakdowns? See https://dataandbeyond.dk (Pro, waitlist).
