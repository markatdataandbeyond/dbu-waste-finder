# DBU Waste Finder MCP (MIT)
Ask Claude/Cursor "why did our Databricks bill jump on Tuesday?" and "fix it" — answered from your own
**system tables**, read-only, and returned as a **diff to your Databricks Asset Bundle**, never applied.

Tools:
- `cost_summary(days)`, `top_cost_drivers(days)`, `explain_cost_spike(date)` – DBUs and list-price USD (USD totals = priced part; `usd_complete: false` + `unpriced_skus` when a SKU has no list price). Missing access returns `errors` with `reason`, `tables` and `fix`, same as `find_waste`.
- `find_waste(days, max_autotermination_minutes=60, warehouse_idle_pct=80, max_rows=50)` – classic: `jobs_on_all_purpose`,
  `missing_autotermination`, `idle_or_oversized`; serverless: `warehouse_long_autostop` (auto-stop 0 or > limit, incl. warehouses
  deleted in the window), `idle_warehouse` (uptime from `system.compute.warehouse_events` vs query time from `system.query.history`,
  idle DBUs × list price of the warehouse's billed SKU), `serverless_jobs` (DBUs per job, failed-run share). At most `max_rows` rows per
  check are returned (`truncated: {check: {shown, total}}`); totals are always computed on all rows (`totals_partial: true` only if a
  check exceeds the 100,000-row fetch cap).
  A check that can't run (no SELECT / schema not enabled) appears under `errors` with `reason`, `tables` and the `fix` (e.g. the GRANT);
  totals are then `null`, not $0. `days` outside 1..365 (or not an integer) gives one clear error and runs no query.
  Prices: `est_monthly_saving_usd` sums the priced items; `usd_complete`, `unpriced_items` and `unpriced_skus` show what is missing.
- `propose_fix(bundle_yaml, issue, resource_key)` – `all_purpose_job` | `no_autotermination` | `warehouse_autostop`
  (sets `resources.sql_warehouses.<key>.auto_stop_mins`, default 10). Returns a diff; nothing is applied.
Costs are at list price (`system.billing.list_prices`), not your discount.

## Privacy
The MCP server runs on your machine and sends query results (resource names/ids, DBUs, list-price amounts, owners' email addresses)
to your AI client and its provider. The notebook, by contrast, keeps everything inside the workspace.

## Requirements
Unity Catalog with `system.billing`, `system.compute`, `system.lakeflow` enabled; a SQL warehouse;
a token for a principal with only `SELECT` on those schemas (use a service principal).

## Install
```bash
git clone https://github.com/markatdataandbeyond/dbu-waste-finder && cd dbu-waste-finder/mcp
python -m venv .venv && .venv/bin/pip install -e .
```
Env: `DATABRICKS_HOST` (e.g. `adb-123.azuredatabricks.net`), `DATABRICKS_HTTP_PATH` (warehouse → Connection details), `DATABRICKS_TOKEN`.

**Two ways to connect** (both read-only):
- `DATABRICKS_WAREHOUSE_ID` set → uses the SQL Statement Execution API over HTTPS (no `DATABRICKS_HTTP_PATH` needed). Env: `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID` (warehouse → Overview, or the id at the end of the HTTP path).
- Otherwise → `databricks-sql-connector` with `DATABRICKS_HTTP_PATH` as above.

### Claude Desktop (`claude_desktop_config.json`)
```json
{"mcpServers": {"dbu-waste-finder": {
  "command": "/abs/path/mcp/.venv/bin/dbu-waste-finder-mcp",
  "env": {"DATABRICKS_HOST": "...", "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/...", "DATABRICKS_TOKEN": "..."}}}}
```
### Cursor (`.cursor/mcp.json` or `~/.cursor/mcp.json`)
Same JSON block as above.

## Tests
`.venv/bin/pip install -e .[test] && .venv/bin/pytest -q` (mocked query results; no workspace needed).

Built on MCP Python SDK v2 (`mcp.server.mcpserver.MCPServer`) and `databricks-sql-connector`.
Queries are shared with `../notebook/dbu_waste_finder.py`. Validated live (trial workspace, Statement API runner, 2026-10-26):
`cost_summary`, `top_cost_drivers`, `explain_cost_spike`, `find_waste` → `idle_warehouse`, `warehouse_long_autostop`, `serverless_jobs`
(runs/failed and the billed DBUs that had landed; see root README). Not yet validated live: classic-cluster checks, idle-warehouse USD, `propose_fix` against a real bundle deploy, and the
`databricks-sql-connector` path.
