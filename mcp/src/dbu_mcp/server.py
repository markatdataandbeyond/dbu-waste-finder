"""DBU Waste Finder MCP server: Databricks cost diagnosis + fix proposals. Read-only."""
import os
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from . import fixes, queries

Runner = Callable[[str], list[dict[str, Any]]]


def databricks_runner() -> Runner:
    """Statement Execution API if DATABRICKS_WAREHOUSE_ID is set, else databricks-sql-connector."""
    if os.environ.get("DATABRICKS_WAREHOUSE_ID"):
        from .stmt_api import statement_runner
        return statement_runner()
    from databricks import sql  # databricks-sql-connector

    host = os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/")
    http_path = os.environ["DATABRICKS_HTTP_PATH"]
    token = os.environ["DATABRICKS_TOKEN"]

    def run(q: str) -> list[dict[str, Any]]:
        with sql.connect(server_hostname=host, http_path=http_path, access_token=token) as c:
            with c.cursor() as cur:
                cur.execute(q)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    return run


def _sum_or_none(vals):
    """Sum of USD values; None (= unknown, list_prices missing) if any value is None. Never a fake $0."""
    vals = list(vals)
    return None if any(v is None for v in vals) else round(sum(float(v) for v in vals), 2)


def _f(x) -> float:
    return float(x or 0)


def build(runner: Runner | None = None) -> MCPServer:
    mcp = MCPServer("dbu-waste-finder", instructions=(
        "Databricks cost diagnosis from system tables (list prices, read-only). "
        "Use find_waste then propose_fix to produce a reviewable Asset Bundle diff."), version="0.1.0")
    state: dict[str, Runner | None] = {"run": runner}

    def run(q: str):
        if state["run"] is None:
            state["run"] = databricks_runner()
        return state["run"](q)

    @mcp.tool()
    def cost_summary(days: int = 30) -> dict:
        """Total list-price cost, cost per product and per day for the last `days` days."""
        by_product = run(queries.render(queries.COST_BY_PRODUCT, days))
        daily = run(queries.render(queries.DAILY_COST, days))
        total = _sum_or_none(r.get("list_cost_usd") for r in by_product)
        out = {"days": days, "total_list_cost_usd": total,
               "total_dbus": round(sum(_f(r.get("dbus")) for r in by_product), 3),
               "by_product": by_product, "daily": daily, "note": "List price, not your negotiated discount."}
        if total is None:
            out["cost_unknown"] = "system.billing.list_prices has no price for some SKUs; USD unknown, see DBUs."
        return out

    @mcp.tool()
    def top_cost_drivers(days: int = 30) -> list[dict]:
        """Top 20 jobs/warehouses/clusters by list-price cost."""
        return run(queries.render(queries.WASTE["top_cost_drivers"], days))

    @mcp.tool()
    def find_waste(days: int = 30, max_autotermination_minutes: int = 60,
                   warehouse_idle_pct: float = 80.0) -> dict:
        """Classic: jobs on all-purpose compute, clusters without/with long auto-termination, idle clusters.
        Serverless: SQL warehouses with no/long auto-stop, idle SQL warehouses (uptime vs query time),
        serverless job cost incl. failed runs. Returns estimated monthly saving in USD (None = unknown
        because list_prices is missing) and wasted DBUs."""
        out, errors = {}, {}
        for k in ("jobs_on_all_purpose", "missing_autotermination", "idle_or_oversized",
                  "warehouse_long_autostop", "idle_warehouse", "serverless_jobs"):
            try:
                out[k] = run(queries.render(queries.WASTE[k], days, maxterm=max_autotermination_minutes,
                                            idle_pct=warehouse_idle_pct))
            except Exception as e:  # e.g. system.compute not enabled
                errors[k] = str(e)[:300]
        usd = [r.get("est_saving_if_job_compute_usd") for r in out.get("jobs_on_all_purpose", [])]
        usd += [r.get("est_saving_usd") for r in out.get("idle_or_oversized", [])]
        usd += [r.get("est_saving_usd") for r in out.get("serverless_jobs", []) if r.get("failed_runs")]
        saving = _sum_or_none(usd)
        out["est_monthly_saving_usd"] = None if saving is None else round(saving * 30.0 / days, 2)
        dbus = sum(_f(r.get("est_idle_dbus")) for r in out.get("idle_warehouse", []))
        dbus += sum(_f(r.get("est_waste_dbus")) for r in out.get("serverless_jobs", []))
        out["est_monthly_waste_dbus"] = round(dbus * 30.0 / days, 2)
        if saving is None:
            out["cost_unknown"] = "Some flagged items have no list price; USD unknown (not $0), see DBUs."
        if out.get("idle_warehouse"):
            out.setdefault("notes", []).append("Idle-warehouse waste is in DBUs only (size-based rate); "
                                               "fix with propose_fix issue='warehouse_autostop'.")
        if errors:
            out["errors"] = errors
        return out

    @mcp.tool()
    def explain_cost_spike(date: str) -> dict:
        """Which resources drove cost on `date` (YYYY-MM-DD) vs the prior 7-day daily average."""
        rows = run(queries.render_spike(date))
        delta = _sum_or_none(r.get("delta_usd") for r in rows)
        return {"date": date, "top_movers": rows, "net_delta_top15_usd": delta,
                "net_delta_top15_dbus": round(sum(_f(r.get("delta_dbus")) for r in rows), 3)}

    @mcp.tool()
    def propose_fix(bundle_yaml: str, issue: str, resource_key: str, path: str = "databricks.yml",
                    spark_version: str | None = None, node_type_id: str | None = None,
                    num_workers: int | None = None, minutes: int | None = None) -> str:
        """Return a unified diff for a Databricks Asset Bundle YAML. issue = 'all_purpose_job'
        (move tasks from existing_cluster_id to a job cluster) or 'no_autotermination'
        (set resources.clusters.<key>.autotermination_minutes) or 'warehouse_autostop'
        (set resources.sql_warehouses.<key>.auto_stop_mins, default 10; for long/no auto-stop or idle
        warehouses). Nothing is applied."""
        return fixes.propose(bundle_yaml, issue, resource_key, path, spark_version=spark_version,
                             node_type_id=node_type_id, num_workers=num_workers, minutes=minutes)

    return mcp


def main() -> None:
    build().run()  # stdio


if __name__ == "__main__":
    main()
