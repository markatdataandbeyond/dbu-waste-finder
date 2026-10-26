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
| Estimated monthly saving | sum of the two saving checks, scaled to 30 days |

## Use
1. Import `notebook/dbu_waste_finder.py` into your workspace (File → Import).
2. Attach to any compute (serverless works) as a user with `SELECT` on `system.billing`, `system.compute`, `system.lakeflow`.
3. Adjust thresholds in the first cell, Run all. Checks whose schema isn't enabled are skipped.

## Caveats
- Costs use `pricing.effective_list.default` (list price), not your contracted discount.
- The idle saving (50 %) is an assumption; tune `IDLE_SAVING_SHARE`. Checks can overlap.
- `usage_metadata.job_id` is not populated for jobs on all-purpose compute (per Databricks docs), so those are detected via `job_task_run_timeline.compute_ids`.

## Sources (fetched 2026-10-26)
- Billable usage: https://docs.databricks.com/aws/en/admin/system-tables/billing
- Pricing: https://docs.databricks.com/aws/en/admin/system-tables/pricing
- Compute: https://docs.databricks.com/aws/en/admin/system-tables/compute
- Jobs: https://docs.databricks.com/aws/en/admin/system-tables/jobs

## Test locally
`pip install duckdb sqlglot && python tests/test_notebook.py` — parses every query as Databricks SQL and runs it on mock tables.

Not affiliated with Databricks. "Databricks" is a trademark of Databricks, Inc. MIT licensed, © 2026 Data and Beyond ApS.
