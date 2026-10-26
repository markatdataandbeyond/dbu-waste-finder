# DBU Waste Finder

A free, read-only Databricks notebook that finds where your DBUs go and what you can cut, using only
Databricks **system tables**. One notebook, no install, no data leaves your workspace.

This repo contains two things:
- **`notebook/`** – the free notebook (below).
- **`mcp/`** – an MCP server so Claude/Cursor can answer "why did our bill jump?" from your system tables and propose fixes as a diff to your Asset Bundle (read-only, never applied). See [`mcp/README.md`](mcp/README.md).

Landing page: https://markatdataandbeyond.github.io/dbu-waste-finder/

## What it checks
| Check | Source tables |
|---|---|
| Cost by product and top 20 cost drivers (jobs, warehouses, clusters) at list price | `system.billing.usage` + `system.billing.list_prices` |
| Jobs running on all-purpose compute, with saving if moved to job compute (ratio from your own `list_prices`) | `system.lakeflow.job_task_run_timeline` (`compute_ids`), `system.compute.clusters` |
| All-purpose clusters with auto-termination off (0/NULL) or above 60 min | `system.compute.clusters` (latest SCD row) |
| Idle or oversized clusters (avg CPU < 10 % and memory < 30 %) | `system.compute.node_timeline` |
| SQL warehouses with auto-stop off (0) or above 60 min, incl. warehouses deleted inside the window (`warehouse_long_autostop`) | `system.compute.warehouses` (latest SCD row) |
| Idle SQL warehouses: uptime (`cluster_count` × time between events) vs minutes with a query running; flagged at ≥ 80 % idle and ≥ 30 min up, waste in DBUs from size (`idle_warehouse`) | `system.compute.warehouse_events`, `system.query.history`, `system.compute.warehouses` |
| Serverless jobs: DBUs and cost per job, runs, failed runs; waste = failed-run share (`serverless_jobs`) | `system.billing.usage` (SKU contains `SERVERLESS`), `system.lakeflow.job_run_timeline` |
| Estimated monthly saving | USD from the saving checks scaled to 30 days, plus wasted DBUs (idle warehouses, failed serverless runs) |

If a SKU has no row in `system.billing.list_prices` (e.g. `GENIE_FREE_USAGE`), its cost is shown as NULL = **unknown**, never $0; DBUs are always shown.

## Use
1. Import `notebook/dbu_waste_finder.py` into your workspace (File → Import).
2. Attach to any compute (serverless works) as a user with `SELECT` on `system.billing`, `system.compute`, `system.lakeflow`.
3. Adjust thresholds in the first cell, Run all. Checks whose schema isn't enabled are skipped.

## Caveats
- Costs use `pricing.effective_list.default` (list price), not your contracted discount.
- The idle saving (50 %) is an assumption; tune `IDLE_SAVING_SHARE`. Checks can overlap.
- Idle-warehouse DBU/h per size (2X-Small 4 … 4X-Large 528) is the published Databricks SQL rate, an assumption; real billed DBUs are in `system.billing.usage`.
- `system.billing.usage` lags by hours; `system.compute` and `system.query` usually land sooner, so warehouse checks can show results before cost does.
- `usage_metadata.job_id` is not populated for jobs on all-purpose compute (per Databricks docs), so those are detected via `job_task_run_timeline.compute_ids`.

## Sources (fetched 2026-10-26)
- Billable usage: https://docs.databricks.com/aws/en/admin/system-tables/billing
- Pricing: https://docs.databricks.com/aws/en/admin/system-tables/pricing
- Compute: https://docs.databricks.com/aws/en/admin/system-tables/compute
- Jobs: https://docs.databricks.com/aws/en/admin/system-tables/jobs

## Test locally
From the repo root (Python ≥ 3.10):
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt -e ./mcp && .venv/bin/pytest -q
```
Runs the notebook test (every query parsed as Databricks SQL with sqlglot and executed on mock system tables in DuckDB),
the MCP server tests (mocked query results) and landing-page checks. No workspace needed.

## Validation status
- Run live against a Databricks trial workspace (AWS, serverless-only) on 2026-10-26: all queries execute; `idle_warehouse`
  found the test warehouse (77 min up, 2 busy min); `warehouse_long_autostop` found it after deletion; cost tools returned DBUs
  with USD unknown for an unpriced SKU.
- Not yet validated live: the classic-cluster checks (jobs on all-purpose, auto-termination, idle clusters) — the trial workspace
  cannot run classic clusters — and `serverless_jobs` USD/DBU figures (billing rows had not landed yet).

Not affiliated with Databricks. "Databricks" is a trademark of Databricks, Inc. MIT licensed, © 2026 Data and Beyond ApS.
