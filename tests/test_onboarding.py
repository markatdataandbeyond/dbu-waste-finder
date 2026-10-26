"""Onboarding docs: one clear connect order, and a read-only grant snippet covering every schema the checks read."""
import pathlib, re

ROOT = pathlib.Path(__file__).parents[1]
MCP = (ROOT / "mcp/README.md").read_text()
README = (ROOT / "README.md").read_text()
NB = (ROOT / "notebook/dbu_waste_finder.py").read_text()
SCHEMAS = sorted(set(re.findall(r"system\.(billing|compute|lakeflow|query)\.", NB)))


def test_schemas_used_by_checks():
    assert SCHEMAS == ["billing", "compute", "lakeflow", "query"]


def test_quickstart_recommends_warehouse_id_first():
    i = MCP.index("## Install")
    sect = MCP[i:MCP.index("### Claude Desktop")]
    assert sect.index("DATABRICKS_WAREHOUSE_ID") < sect.index("DATABRICKS_HTTP_PATH")
    assert "recommended" in sect.lower()
    # config example must use the recommended path
    cfg = MCP[MCP.index("### Claude Desktop"):]
    assert '"DATABRICKS_WAREHOUSE_ID"' in cfg.split("```")[1]


def test_grant_snippet_is_read_only_and_complete():
    blocks = re.findall(r"```sql\n(.*?)```", MCP, re.S)
    assert blocks, "no ```sql grant block"
    g = blocks[0]
    assert "GRANT USE CATALOG ON CATALOG system TO `<sp-application-id>`;" in g
    for s in SCHEMAS:
        assert f"GRANT USE SCHEMA, SELECT ON SCHEMA system.{s} TO `<sp-application-id>`;" in g
    assert not re.search(r"\b(MODIFY|ALL PRIVILEGES|CREATE|MANAGE)\b", g)
    assert "CAN USE" in MCP          # warehouse permission is not a SQL grant


def test_both_readmes_list_query_schema():
    for doc in (README, MCP):
        assert "system.query" in doc
