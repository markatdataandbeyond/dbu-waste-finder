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
  Totals: `measured_saving_usd`/`measured_waste_dbu` (window), `observation_days` (actual data coverage), `projected_30d_usd`/`_dbu`
  (= measured ÷ observation_days × 30, null + `projection_withheld` below 3 days) and `low_confidence` (< 7 days); see root README. USD covers priced items; `usd_complete`, `unpriced_items` and `unpriced_skus` show what is missing.
- `cost_review_report(days, …)` – one markdown cost-review (executive summary, top drivers, waste findings with estimated list-price amounts, proposed fixes) from the three tools above; proposals only.
- `propose_fix(bundle_yaml, issue, resource_key, cluster_id=None)` – `all_purpose_job` | `no_autotermination` | `warehouse_autostop`
  (sets `resources.sql_warehouses.<key>.auto_stop_mins`, default 10). Returns a diff; nothing is applied. With `cluster_id`, a cluster that has been deleted is refused with a clear message.
Costs are at list price (`system.billing.list_prices`), not your discount.

## Privacy
- **Notebook:** runs inside your workspace, read-only; nothing leaves the workspace.
- **MCP server:** runs on your machine and sends its query results (job/cluster/warehouse names and ids, DBUs, list-price amounts,
  owners' email addresses) to your AI client and thereby to that AI provider. Use a read-only service principal and only connect it
  to an AI client you may share that metadata with. Nothing is sent to Data and Beyond.

## Requirements
Unity Catalog with `system.billing`, `system.compute`, `system.lakeflow`, `system.query` enabled; a SQL warehouse;
a token for a principal with only `SELECT` on those schemas (use a service principal).

Read-only grants for a service principal (run as a metastore/account admin; use the SP's application ID):
```sql
GRANT USE CATALOG ON CATALOG system TO `<sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.billing TO `<sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.compute TO `<sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.lakeflow TO `<sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.query TO `<sp-application-id>`;
```
Then give the SP **CAN USE** on the SQL warehouse (warehouse → Permissions). Nothing else is needed.

## Install
```bash
git clone https://github.com/markatdataandbeyond/dbu-waste-finder && cd dbu-waste-finder/mcp
python3 -m venv .venv && .venv/bin/pip install -e .
```
**Connect** (both read-only):
1. **Recommended:** `DATABRICKS_HOST` (e.g. `adb-123.azuredatabricks.net`), `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID`
   (warehouse → Overview, or the id at the end of the HTTP path). Uses the SQL Statement Execution API over HTTPS.
2. Fallback, only if `DATABRICKS_WAREHOUSE_ID` is not set: `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_HTTP_PATH`
   (warehouse → Connection details), via `databricks-sql-connector`.

### Claude Desktop (`claude_desktop_config.json`)
```json
{"mcpServers": {"dbu-waste-finder": {
  "command": "/abs/path/mcp/.venv/bin/dbu-waste-finder-mcp",
  "env": {"DATABRICKS_HOST": "...", "DATABRICKS_WAREHOUSE_ID": "...", "DATABRICKS_TOKEN": "..."}}}}
```
### Cursor (`.cursor/mcp.json` or `~/.cursor/mcp.json`)
Same JSON block as above.


## Docker
```bash
# from repo root
docker build -t dbu-waste-finder-mcp .
docker run --rm -i \
  -e DATABRICKS_HOST=... -e DATABRICKS_TOKEN=... -e DATABRICKS_WAREHOUSE_ID=... \
  dbu-waste-finder-mcp
```
The image starts the stdio MCP server (`dbu-waste-finder-mcp`). Credentials are only required when tools are called; `initialize` / `tools/list` work without them (Glama introspection).

## Tests
`.venv/bin/pip install -e .[test] && .venv/bin/pytest -q` (mocked query results; no workspace needed).

Built on MCP Python SDK v2 (`mcp.server.mcpserver.MCPServer`) and `databricks-sql-connector`.
Queries are shared with `../notebook/dbu_waste_finder.py`. Validated live (trial workspace, Statement API runner, 2026-10-26):
`cost_summary`, `top_cost_drivers`, `explain_cost_spike`, `find_waste` → `idle_warehouse`, `warehouse_long_autostop`, `serverless_jobs`
(runs/failed and the billed DBUs that had landed; see root README). Not yet validated live: classic-cluster checks, `propose_fix` against a real bundle deploy, and the
`databricks-sql-connector` path.
