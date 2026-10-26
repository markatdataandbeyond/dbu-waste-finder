"""SQL against Databricks system tables (read-only).

PRICED_USAGE and the waste queries are copied verbatim from ../../notebook/dbu_waste_finder.py
(tests/test_queries.py asserts they stay identical). Column names verified against
docs.databricks.com admin/system-tables (billing, pricing, compute, jobs), fetched 2026-10-26.
"""
import datetime as _dt
import importlib.util as _ilu
import pathlib as _pl

_NB = _pl.Path(__file__).resolve().parents[3] / "notebook" / "dbu_waste_finder.py"


def _load_notebook():
    spec = _ilu.spec_from_file_location("dbu_waste_finder", _NB)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:  # inside the repo: reuse the notebook's queries directly (single source of truth)
    _nb = _load_notebook()
    PRICED_USAGE = _nb.PRICED_USAGE
    WASTE = {k: _nb.QUERIES[k] for k in ("top_cost_drivers", "missing_autotermination",
                                          "jobs_on_all_purpose", "idle_or_oversized",
                                          "warehouse_long_autostop", "idle_warehouse", "serverless_jobs")}
    COST_BY_PRODUCT = _nb.QUERIES["cost_by_product"]
except FileNotFoundError:  # pragma: no cover - packaged install ships _vendored.py
    from ._vendored import PRICED_USAGE, WASTE, COST_BY_PRODUCT  # type: ignore

DAILY_COST = PRICED_USAGE + """
SELECT usage_date, ROUND(SUM(dbus), 3) AS dbus,
       CASE WHEN COUNT(list_cost) = COUNT(*) THEN ROUND(SUM(list_cost), 2) END AS list_cost_usd
FROM priced GROUP BY usage_date ORDER BY usage_date"""

# Cost per resource on one day vs the average of the 7 days before it.
SPIKE = """
WITH priced AS (
  SELECT u.usage_date, u.billing_origin_product,
         COALESCE(u.usage_metadata.job_id, u.usage_metadata.warehouse_id,
                  u.usage_metadata.cluster_id, u.billing_origin_product) AS resource_id,
         u.usage_quantity AS dbus,
         u.usage_quantity * CAST(p.pricing.effective_list.default AS DOUBLE) AS list_cost
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date BETWEEN date_add(DATE'{day}', -7) AND DATE'{day}'
), agg AS (
  SELECT billing_origin_product, resource_id,
         SUM(CASE WHEN usage_date = DATE'{day}' THEN dbus ELSE 0 END) AS day_dbus,
         SUM(CASE WHEN usage_date < DATE'{day}' THEN dbus ELSE 0 END) / 7.0 AS baseline_dbus,
         SUM(CASE WHEN usage_date = DATE'{day}' THEN list_cost ELSE 0 END) AS day_cost,
         SUM(CASE WHEN usage_date < DATE'{day}' THEN list_cost ELSE 0 END) / 7.0 AS baseline_cost,
         COUNT(list_cost) = COUNT(*) AS priced_ok
  FROM priced GROUP BY 1, 2
)
SELECT billing_origin_product, resource_id,
       ROUND(day_dbus, 3) AS day_dbus, ROUND(day_dbus - baseline_dbus, 3) AS delta_dbus,
       CASE WHEN priced_ok THEN ROUND(day_cost, 2) END AS day_cost_usd,
       CASE WHEN priced_ok THEN ROUND(baseline_cost, 2) END AS baseline_daily_usd,
       CASE WHEN priced_ok THEN ROUND(day_cost - baseline_cost, 2) END AS delta_usd
FROM agg WHERE day_dbus - baseline_dbus <> 0
ORDER BY delta_usd DESC NULLS LAST, delta_dbus DESC LIMIT 15"""


def render(sql: str, days: int = 30, maxterm: int = 60, idle_share: float = 0.5,
           cpu: float = 10.0, mem: float = 30.0, idle_pct: float = 80.0, min_up: int = 30) -> str:
    days = int(days)
    if not 1 <= days <= 365:
        raise ValueError("days must be 1..365")
    return sql.format(d=days, maxterm=int(maxterm), idle_share=float(idle_share),
                      cpu=float(cpu), mem=float(mem), idle_pct=float(idle_pct), min_up=int(min_up))


def render_spike(day: str) -> str:
    d = _dt.date.fromisoformat(day)  # validates -> no SQL injection via the date
    return SPIKE.format(day=d.isoformat())
