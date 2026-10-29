"""DBU Waste Finder MCP server: Databricks cost diagnosis + fix proposals. Read-only."""
import os
import re
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import cost_review, delta_maintenance, fixes, queries

FETCH_CAP = 100_000  # safety cap on rows fetched per check (totals are marked partial if hit)
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


_TABLE_RE = re.compile(r"\bsystem\.[a-z_]+\.[a-z_]+\b")


def _access_error(q: str, e: Exception) -> dict:
    """Turn a failed check into a concrete, actionable error: which system tables, why, how to fix."""
    msg = str(e)
    tables = sorted(set(_TABLE_RE.findall(q)))
    up = msg.upper()
    if "PERMISSION" in up or "NOT AUTHORIZED" in up or "ACCESS DENIED" in up or "INSUFFICIENT" in up:
        reason = "permission_denied"
        hit = [t for t in tables if t in msg] or tables
        fix = "; ".join(f"GRANT SELECT ON TABLE {t} TO <principal>" for t in hit) + \
              " (plus USE CATALOG system and USE SCHEMA on the schema)"
        tables = hit
    elif "NOT_FOUND" in up or "NOT FOUND" in up or "CANNOT BE FOUND" in up or "DOES NOT EXIST" in up:
        reason = "table_not_found"
        fix = ("Enable the system schema (account admin: Catalog Explorer > system, or "
               "`databricks system-schemas enable`) for: " + ", ".join(sorted({t.rsplit('.', 1)[0] for t in tables})))
    else:
        reason = "query_failed"
        fix = "Check the SQL warehouse and that these tables are readable: " + ", ".join(tables)
    return {"reason": reason, "tables": tables, "fix": fix, "error": msg[:300]}


def _norm(row: dict) -> dict:
    """Statement API returns booleans as 'true'/'false' strings; make *_complete real booleans."""
    return {k: (v.lower() == "true" if isinstance(v, str) and k.endswith("_complete") else v) for k, v in row.items()}


def _known_sum(vals) -> float:
    """Sum of the USD values that are known (priced). Missing prices are reported separately."""
    return round(sum(float(v) for v in vals if v is not None), 2)


MIN_CONFIDENT_DAYS = 7
MIN_PROJECTION_DAYS = 3     # below this no 30-day projection is shown (projection_withheld)


def _project(measured_usd, measured_dbu, observation_days, no_data=False) -> dict:
    """Same rule as the notebook's project(): measured / observation_days * 30; low_confidence below 7 days."""
    obs = None if observation_days is None else float(observation_days)
    have = "unknown" if obs is None else f"{obs:g}"
    shown = obs is not None and obs >= MIN_PROJECTION_DAYS
    out = {"measured_saving_usd": measured_usd, "measured_waste_dbu": measured_dbu, "observation_days": obs,
           "projected_30d_usd": round(measured_usd / obs * 30, 2) if shown and measured_usd is not None else None,
           "projected_30d_dbu": round(measured_dbu / obs * 30, 2) if shown and measured_dbu is not None else None,
           "low_confidence": obs is None or obs < MIN_CONFIDENT_DAYS}
    if not shown:
        out["projection_withheld"] = f"need >={MIN_PROJECTION_DAYS} days, have {have}"
    if out["low_confidence"]:
        days_txt = ("no usage records in the window yet (new workspace or billing lag)" if obs is None and no_data
                    else "observation_days unknown (data could not be read)" if obs is None else f"only {have} days of data")
        out["note"] = (f"low_confidence: {days_txt} (fewer than {MIN_CONFIDENT_DAYS}); "
                       + ("no 30-day projection below 3 days." if not shown
                          else "the 30-day projection is a rough extrapolation, not a forecast."))
    return out


def _days(days) -> int:
    try:
        return queries.validate_days(days)
    except ValueError as e:
        raise ToolError(str(e)) from None  # ToolError text reaches the client (not the generic crash text)


def _f(x) -> float:
    return float(x or 0)


def build(runner: Runner | None = None) -> MCPServer:
    mcp = MCPServer("dbu-waste-finder", instructions=(
        "Read-only Databricks cost/waste diagnosis from system tables at list price. "
        "Typical flow: cost_summary or top_cost_drivers for orientation, find_waste or "
        "cost_review_report for findings, then propose_fix for a reviewable Asset Bundle "
        "diff or Delta SQL. Never applies changes; never writes to the workspace."), version="0.1.0")
    state: dict[str, Runner | None] = {"run": runner}

    def run(q: str):
        if state["run"] is None:
            state["run"] = databricks_runner()
        return [_norm(r) for r in state["run"](q)]

    def unpriced_skus(days: int):
        """SKUs used in the window without a list price -> (rows, error)."""
        q = queries.render(queries.UNPRICED, days)
        try:
            return run(q), None
        except Exception as e:
            return [], _access_error(q, e)

    @mcp.tool()
    def cost_summary(days: int = 30) -> dict:
        """Orient on spend: total list-price cost, cost per product and per day for the last `days` days
        (integer 1..365). Read-only; list price, not your discount. USD totals cover priced usage only —
        when a SKU has no list price, `usd_complete` is false and `unpriced_skus` lists it. Use before
        find_waste when you need the overall bill shape."""
        days = _days(days)
        try:
            q = queries.render(queries.COST_BY_PRODUCT, days); by_product = run(q)
            q = queries.render(queries.DAILY_COST, days); daily = run(q)
        except Exception as e:
            return {"days": days, "total_list_cost_usd": None, "errors": {"cost_summary": _access_error(q, e)}}
        skus, err = unpriced_skus(days)
        out = {"days": days, "total_list_cost_usd": _known_sum(r.get("list_cost_usd") for r in by_product),
               "usd_complete": not skus and err is None and all(r.get("usd_complete", True) for r in by_product),
               "unpriced_skus": skus, "total_dbus": round(sum(_f(r.get("dbus")) for r in by_product), 3),
               "by_product": by_product, "daily": daily, "note": "List price, not your negotiated discount."}
        if err:
            out["errors"] = {"unpriced_skus": err}
        return out

    @mcp.tool()
    def top_cost_drivers(days: int = 30) -> dict:
        """Top 20 jobs, warehouses and clusters by list-price cost over the last `days` days (1..365).
        Read-only; returns `{rows, usd_complete, unpriced_skus}`. Use to answer "what spent the most?"
        before drilling into waste with find_waste. List price only — not applied discounts."""
        days = _days(days)
        q = queries.render(queries.WASTE["top_cost_drivers"], days)
        try:
            rows = run(q)
        except Exception as e:
            return {"errors": {"top_cost_drivers": _access_error(q, e)}}
        skus, err = unpriced_skus(days)
        out = {"rows": rows, "usd_complete": not skus and err is None and all(r.get("usd_complete", True) for r in rows),
               "unpriced_skus": skus}
        if err:
            out["errors"] = {"unpriced_skus": err}
        return out

    @mcp.tool()
    def cost_review_report(days: int = 30, max_autotermination_minutes: int = 60,
                           warehouse_idle_pct: float = 80.0, max_rows: int = 50) -> dict:
        """One markdown cost-review report for the last `days` days (1..365) from cost_summary +
        top_cost_drivers + find_waste: executive summary, top drivers, waste findings with estimated
        list-price amounts, and proposed fixes. `max_autotermination_minutes` / `warehouse_idle_pct` /
        `max_rows` are forwarded to find_waste. Read-only; proposals only — nothing is applied.
        Returns `{days, markdown, measured_saving_usd, projected_30d_usd}`."""
        from datetime import datetime, timezone
        summary = cost_summary(days)
        drivers = top_cost_drivers(days)
        waste = find_waste(days, max_autotermination_minutes=max_autotermination_minutes,
                           warehouse_idle_pct=warehouse_idle_pct, max_rows=max_rows)
        md = cost_review.generate_report(
            summary, drivers, waste,
            as_of=datetime.now(timezone.utc).date().isoformat(),
        )
        return {
            "days": days,
            "markdown": md,
            "measured_saving_usd": waste.get("measured_saving_usd"),
            "projected_30d_usd": waste.get("projected_30d_usd"),
            "observation_days": waste.get("observation_days"),
            "usd_complete": waste.get("usd_complete"),
        }

    @mcp.tool()
    def find_waste(days: int = 30, max_autotermination_minutes: int = 60,
                   warehouse_idle_pct: float = 80.0, max_rows: int = 50) -> dict:
        """Find wasted DBUs over the last `days` days (1..365). Read-only; never changes the workspace.
        Returns measured_saving_usd / measured_waste_dbu, observation_days, projected_30d_usd/_dbu
        (= measured / observation_days * 30; withheld below 3 days) and low_confidence (< 7 days).
        Classic: jobs on all-purpose, clusters with auto-termination off or above
        `max_autotermination_minutes` (default 60), idle/oversized clusters. Serverless: warehouses with
        no/long auto-stop, idle warehouses (idle_pct ≥ `warehouse_idle_pct`, default 80), serverless jobs
        incl. failed-run share. Delta: needs OPTIMIZE / VACUUM / over-frequent OPTIMIZE (proposals only).
        At most `max_rows` rows per check (1..1000). USD is None when unknown; failed checks go under
        `errors` with the table and the GRANT/fix — totals are never a fake $0."""
        days = _days(days)
        max_rows = int(max_rows)
        if not 1 <= max_rows <= 1000:
            raise ToolError(f"max_rows must be an integer 1..1000, got {max_rows!r}")
        out, errors, truncated, full, capped = {}, {}, {}, {}, {}
        for k in ("jobs_on_all_purpose", "missing_autotermination", "idle_or_oversized",
                  "warehouse_long_autostop", "idle_warehouse", "serverless_jobs",
                  "delta_needs_optimize", "delta_needs_vacuum", "delta_overfrequent_optimize"):
            q = queries.render(queries.WASTE[k], days, maxterm=max_autotermination_minutes,
                               idle_pct=warehouse_idle_pct) + f"\nLIMIT {FETCH_CAP + 1}"
            try:
                rows = run(q)
            except Exception as e:  # e.g. system schema not enabled / no SELECT
                errors[k] = _access_error(q, e)
                continue
            if len(rows) > FETCH_CAP:
                rows, capped[k] = rows[:FETCH_CAP], True
            full[k] = rows
            if len(rows) > max_rows:
                truncated[k] = {"shown": max_rows, "total": f">{FETCH_CAP}" if k in capped else len(rows)}
            out[k] = rows[:max_rows]
        # totals over ALL fetched rows (not only the max_rows shown)
        usd = [r.get("est_saving_if_job_compute_usd") for r in full.get("jobs_on_all_purpose", [])]
        usd += [r.get("est_saving_usd") for r in full.get("idle_or_oversized", [])]
        usd += [r.get("est_saving_usd") for r in full.get("serverless_jobs", []) if r.get("failed_runs")]
        usd += [r.get("est_idle_cost_usd") for r in full.get("idle_warehouse", [])]
        saving = _known_sum(usd)
        saving_checks = {"jobs_on_all_purpose", "idle_or_oversized", "serverless_jobs", "idle_warehouse"}
        dbu_checks = {"idle_warehouse", "serverless_jobs"}
        all_failed = saving_checks <= set(errors)
        measured_usd = None if all_failed else saving
        if dbu_checks <= set(errors):
            measured_dbu = None
        else:
            measured_dbu = sum(_f(r.get("est_idle_dbus")) for r in full.get("idle_warehouse", []))
            measured_dbu += sum(_f(r.get("est_waste_dbus")) for r in full.get("serverless_jobs", []))
            measured_dbu = round(measured_dbu, 2)
        q = queries.render(queries.OBSERVATION, days)
        try:
            obs_rows = run(q)
        except Exception as e:
            obs_rows, errors["observation"] = [], _access_error(q, e)
        obs = obs_rows[0].get("observation_days") if obs_rows else None
        proj = _project(measured_usd, measured_dbu, obs, no_data=bool(obs_rows) and obs is None)
        note = proj.pop("note", None)
        out.update(proj)
        if note:
            out.setdefault("notes", []).append(note)
        items = []
        for k, col in (("jobs_on_all_purpose", "est_saving_if_job_compute_usd"), ("idle_or_oversized", "est_saving_usd"),
                       ("idle_warehouse", "est_idle_cost_usd"), ("serverless_jobs", "est_saving_usd")):
            for r in full.get(k, []):
                if r.get(col) is None and (k != "serverless_jobs" or r.get("failed_runs")):
                    items.append({"check": k, "id": next((r[c] for c in ("cluster_id", "warehouse_id", "job_id")
                                                          if r.get(c)), None)})
        skus, err = unpriced_skus(days)
        if err:
            errors["unpriced_skus"] = err
        out["unpriced_items"], out["unpriced_skus"] = items, skus
        out["usd_complete"] = not items and not skus and not errors
        if all_failed:
            out["cost_unknown"] = "USD unknown (not $0): every saving check failed, see errors"
        elif not out["usd_complete"]:
            out["usd_note"] = ("measured_saving_usd/projected_30d_usd cover priced items only; see unpriced_items/"
                               "unpriced_skus" + (" and errors" if errors else ""))
        if errors:
            out.setdefault("notes", []).append(
                f"{len([k for k in errors if k not in ('unpriced_skus', 'observation')])} of 9 checks could not run; totals cover only the checks that ran.")
        if out.get("delta_needs_optimize") or out.get("delta_needs_vacuum") or out.get("delta_overfrequent_optimize"):
            out.setdefault("notes", []).append(
                "Delta maintenance findings are proposals only. Use propose_fix with issue="
                "'delta_optimize'|'delta_vacuum'|'delta_zorder'|'delta_predictive_optimization'|"
                "'delta_overfrequent_optimize' to get reviewable SQL; nothing is executed.")
        if out.get("idle_warehouse"):
            out.setdefault("notes", []).append("Idle-warehouse waste = idle DBUs (size-based rate) x list price of the "
                                               "warehouse's billed SKU; USD null until its usage is in system.billing. "
                                               "Fix with propose_fix issue='warehouse_autostop'.")
        q = queries.render(queries.REGION_GAP, days) + f"\nLIMIT {FETCH_CAP + 1}"
        try:
            gap = run(q)
        except Exception as e:  # informational only: does not change the totals
            gap = None
            out.setdefault("warnings", {})["cluster_region_gap"] = _access_error(q, e)
        out["cluster_region_gap"] = gap
        if gap:
            out.setdefault("notes", []).append(
                "Region gap: classic-cluster usage is billed in workspaces "
                f"{[r.get('workspace_id') for r in gap]} that are not visible in this region's system.compute tables, "
                "so the cluster checks did not cover them (system.billing is account-wide, system.compute/lakeflow "
                "are regional). Run against a warehouse in each region.")
        out["totals_partial"] = bool(capped)
        if capped:
            out.setdefault("notes", []).append(f"Totals are partial: {sorted(capped)} hit the {FETCH_CAP}-row fetch cap.")
        if truncated:
            out["truncated"] = truncated
        if errors:
            out["errors"] = errors
        return out

    @mcp.tool()
    def explain_cost_spike(date: str) -> dict:
        """Explain a one-day cost spike: which jobs/warehouses/clusters drove list-price cost on `date`
        (YYYY-MM-DD UTC billing day = system.billing.usage.usage_date) vs the prior 7-day daily average.
        Read-only. Use when the user asks "why did Tuesday cost more?" after cost_summary."""
        try:
            q = queries.render_spike(date)
        except ValueError as e:
            raise ToolError(f"date must be YYYY-MM-DD (a UTC billing day, no time or offset): {e}") from None
        try:
            rows = run(q)
        except Exception as e:
            return {"date": date, "net_delta_top15_usd": None, "errors": {"explain_cost_spike": _access_error(q, e)}}
        unpriced = sorted({str(r.get("resource_id")) for r in rows if not r.get("usd_complete", True)})
        return {"date": date, "top_movers": rows, "net_delta_top15_usd": _known_sum(r.get("delta_usd") for r in rows),
                "usd_complete": not unpriced, "unpriced_resources": unpriced,
                "net_delta_top15_dbus": round(sum(_f(r.get("delta_dbus")) for r in rows), 3)}

    @mcp.tool()
    def propose_fix(bundle_yaml: str, issue: str, resource_key: str, path: str = "databricks.yml",
                    spark_version: str | None = None, node_type_id: str | None = None,
                    num_workers: int | None = None, minutes: int | None = None,
                    cluster_id: str | None = None) -> str:
        """Propose a fix as a unified diff (Asset Bundle YAML) or reviewable SQL — never applied.
        `issue`: 'all_purpose_job' (move tasks off existing_cluster_id onto a job cluster),
        'no_autotermination' (set resources.clusters.<key>.autotermination_minutes),
        'warehouse_autostop' (set resources.sql_warehouses.<key>.auto_stop_mins, default 10),
        or Delta (`delta_optimize`|`delta_vacuum`|`delta_zorder`|`delta_predictive_optimization`|
        `delta_overfrequent_optimize`) which returns SQL (`resource_key` = catalog.schema.table;
        for `delta_zorder` pass columns as comma-separated `node_type_id`; for `delta_vacuum` pass
        retention hours as `minutes`, default 168 / min 7 days). Pass `cluster_id` from find_waste for
        cluster fixes — a deleted cluster is refused. `bundle_yaml` may be empty for Delta issues."""
        warning = ""
        if cluster_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cluster_id)):
                raise ToolError(f"cluster_id must be letters, digits, '-' or '_' (max 64), got {cluster_id!r}")
            q = ("SELECT cluster_id, delete_time FROM (SELECT cluster_id, delete_time, ROW_NUMBER() OVER "
                 "(PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) AS rn FROM system.compute.clusters "
                 f"WHERE cluster_id = '{cluster_id}') x WHERE rn = 1")
            try:
                rows = run(q)
            except Exception as e:
                rows = None
                warning = (f"# warning: could not check whether cluster {cluster_id} still exists "
                           f"({str(e)[:120]}); review before applying.\n")
            if rows is not None:
                gone = [r for r in rows if r.get("delete_time")]
                if gone and len(gone) == len(rows):
                    raise ToolError(f"cluster {cluster_id} was deleted at {gone[0]['delete_time']}; no fix proposed "
                                    "(nothing left to change). If the bundle still defines it, remove it there instead.")
                if not rows:
                    warning = (f"# warning: could not check cluster {cluster_id}: not found in system.compute.clusters "
                               "(wrong id, or a workspace in another region); review before applying.\n")
        if issue in delta_maintenance.ISSUES:
            try:
                zcols = None
                if issue == "delta_zorder":
                    raw = (node_type_id or spark_version or "")
                    zcols = [c.strip() for c in raw.split(",") if c.strip()]
                retention = int(minutes) if minutes is not None else delta_maintenance.MIN_RETENTION_HOURS
                return delta_maintenance.propose_sql(
                    issue, resource_key, zorder_columns=zcols, retention_hours=retention)
            except Exception as e:
                detail = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
                raise ToolError(detail) from None
        try:
            return warning + fixes.propose(bundle_yaml, issue, resource_key, path, spark_version=spark_version,
                                           node_type_id=node_type_id, num_workers=num_workers, minutes=minutes)
        except Exception as e:  # KeyError/ValueError/YAML errors: ToolError text reaches the client
            detail = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            raise ToolError(detail) from None

    return mcp


def main() -> None:
    build().run()  # stdio


if __name__ == "__main__":
    main()
