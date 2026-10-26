# DBU Waste Finder MCP (MIT)
Ask Claude/Cursor "why did our Databricks bill jump on Tuesday?" and "fix it" — answered from your own
**system tables**, read-only, and returned as a **diff to your Databricks Asset Bundle**, never applied.

Tools: `cost_summary(days)`, `top_cost_drivers(days)`, `find_waste(days)` (jobs on all-purpose compute,
missing/long auto-termination, idle clusters + est. monthly saving), `explain_cost_spike(date)`,
`propose_fix(bundle_yaml, issue, resource_key)` (`all_purpose_job` | `no_autotermination`).
Costs are at list price (`system.billing.list_prices`), not your discount.

## Requirements
Unity Catalog with `system.billing`, `system.compute`, `system.lakeflow` enabled; a SQL warehouse;
a token for a principal with only `SELECT` on those schemas (use a service principal).

## Install
```bash
git clone https://github.com/markatdataandbeyond/dbu-waste-finder && cd dbu-waste-finder/mcp
python -m venv .venv && .venv/bin/pip install -e .
```
Env: `DATABRICKS_HOST` (e.g. `adb-123.azuredatabricks.net`), `DATABRICKS_HTTP_PATH` (warehouse → Connection details), `DATABRICKS_TOKEN`.

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
Queries are shared with `../notebook/dbu_waste_finder.py`. Not yet validated against a live workspace.
