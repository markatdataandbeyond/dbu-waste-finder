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
        total = round(sum(_f(r["list_cost_usd"]) for r in by_product), 2)
        return {"days": days, "total_list_cost_usd": total, "by_product": by_product,
                "daily": daily, "note": "List price, not your negotiated discount."}

    @mcp.tool()
    def top_cost_drivers(days: int = 30) -> list[dict]:
        """Top 20 jobs/warehouses/clusters by list-price cost."""
        return run(queries.render(queries.WASTE["top_cost_drivers"], days))

    @mcp.tool()
    def find_waste(days: int = 30, max_autotermination_minutes: int = 60) -> dict:
        """Jobs on all-purpose compute, clusters without/with long auto-termination, idle clusters,
        with estimated monthly saving."""
        out, errors = {}, {}
        for k in ("jobs_on_all_purpose", "missing_autotermination", "idle_or_oversized"):
            try:
                out[k] = run(queries.render(queries.WASTE[k], days, maxterm=max_autotermination_minutes))
            except Exception as e:  # e.g. system.compute not enabled
                errors[k] = str(e)[:300]
        saving = sum(_f(r.get("est_saving_if_job_compute_usd")) for r in out.get("jobs_on_all_purpose", []))
        saving += sum(_f(r.get("est_saving_usd")) for r in out.get("idle_or_oversized", []))
        out["est_monthly_saving_usd"] = round(saving * 30.0 / days, 2)
        if errors:
            out["errors"] = errors
        return out

    @mcp.tool()
    def explain_cost_spike(date: str) -> dict:
        """Which resources drove cost on `date` (YYYY-MM-DD) vs the prior 7-day daily average."""
        rows = run(queries.render_spike(date))
        delta = round(sum(_f(r["delta_usd"]) for r in rows), 2)
        return {"date": date, "top_movers": rows, "net_delta_top15_usd": delta}

    @mcp.tool()
    def propose_fix(bundle_yaml: str, issue: str, resource_key: str, path: str = "databricks.yml",
                    spark_version: str | None = None, node_type_id: str | None = None,
                    num_workers: int | None = None, minutes: int | None = None) -> str:
        """Return a unified diff for a Databricks Asset Bundle YAML. issue = 'all_purpose_job'
        (move tasks from existing_cluster_id to a job cluster) or 'no_autotermination'
        (set resources.clusters.<key>.autotermination_minutes). Nothing is applied."""
        return fixes.propose(bundle_yaml, issue, resource_key, path, spark_version=spark_version,
                             node_type_id=node_type_id, num_workers=num_workers, minutes=minutes)

    return mcp


def main() -> None:
    build().run()  # stdio


if __name__ == "__main__":
    main()
