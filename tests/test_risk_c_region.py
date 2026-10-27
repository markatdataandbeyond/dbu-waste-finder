"""Risk c) system.billing is account-wide but system.compute.* / lakeflow are regional: clusters billed in a
workspace in another region are invisible to the cluster checks. Report that gap explicitly and warn."""
import asyncio, json, pathlib, runpy, sys
import pytest
from test_notebook import make_db, NB, _f

pytest.importorskip("duckdb")
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "mcp/src"))
from dbu_mcp.server import build  # noqa: E402
from test_overnight import call  # noqa: E402

FAR = """INSERT INTO billing_usage (workspace_id,sku_name,cloud,usage_unit,usage_quantity,usage_date,usage_end_time,
  billing_origin_product,usage_metadata,usage_start_time) VALUES
 ('w_eu','PREMIUM_ALL_PURPOSE_COMPUTE','AWS','DBU',10.0, current_date-1, current_timestamp-INTERVAL 1 DAY,'ALL_PURPOSE',
  {'cluster_id':'c_far','job_id':NULL,'warehouse_id':NULL}, current_timestamp-INTERVAL 25 HOUR)"""


def test_region_gap_lists_workspace_with_unseen_clusters():
    res = runpy.run_path(NB)["run_all"](make_db(extra=[FAR]))
    gap = res["cluster_region_gap"]
    assert [r["workspace_id"] for r in gap] == ["w_eu"]
    assert gap[0]["clusters"] == 1 and _f(gap[0]["dbus"]) == 10.0 and _f(gap[0]["list_cost_usd"]) == 5.5


def test_no_gap_when_all_billed_workspaces_are_visible():
    assert runpy.run_path(NB)["run_all"](make_db())["cluster_region_gap"] == []


def test_mcp_find_waste_warns_about_region_gap():
    fw = call(build(make_db(extra=[FAR])), "find_waste", days=30)
    assert [r["workspace_id"] for r in fw["cluster_region_gap"]] == ["w_eu"]
    assert any("w_eu" in n and "region" in n.lower() for n in fw["notes"])
    assert fw["usd_complete"] is True or "cluster_region_gap" not in (fw.get("errors") or {})


def test_mcp_find_waste_no_region_note_without_gap():
    fw = call(build(make_db()), "find_waste", days=30)
    assert fw.get("cluster_region_gap") == [] and not any("region" in n.lower() for n in fw.get("notes", []))
