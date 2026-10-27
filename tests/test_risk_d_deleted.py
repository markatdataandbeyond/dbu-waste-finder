"""Risk d) propose_fix must refuse to propose a fix for a cluster that has been deleted."""
import asyncio, pathlib, sys
import pytest
from test_notebook import make_db
from test_cluster_edges import EXTRA_CLUSTERS

pytest.importorskip("duckdb")
sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "mcp/src"))
from dbu_mcp.server import build  # noqa: E402

Y = ("resources:\n  clusters:\n    c1:\n      autotermination_minutes: 0\n"
     "  jobs:\n    j1:\n      tasks:\n      - task_key: t\n        existing_cluster_id: c_del\n")


def _call(run, **a):
    return asyncio.run(build(run).call_tool("propose_fix", {"bundle_yaml": Y, **a}))


@pytest.mark.parametrize("issue,key", [("no_autotermination", "c1"), ("all_purpose_job", "j1")])
def test_deleted_cluster_is_refused(issue, key):
    with pytest.raises(Exception) as ei:
        _call(make_db(extra=[EXTRA_CLUSTERS]), issue=issue, resource_key=key, cluster_id="c_del")
    msg = str(ei.value)
    assert "c_del" in msg and "deleted" in msg and "no fix" in msg.lower()


def test_live_cluster_still_gets_diff():
    r = _call(make_db(extra=[EXTRA_CLUSTERS]), issue="no_autotermination", resource_key="c1", cluster_id="c_ap")
    assert "+      autotermination_minutes: 20" in r.content[0].text


def test_cluster_id_is_validated():
    with pytest.raises(Exception) as ei:
        _call(make_db(), issue="no_autotermination", resource_key="c1", cluster_id="x' OR 1=1 --")
    assert "cluster_id" in str(ei.value)


def test_lookup_failure_still_proposes_with_warning():
    def broken(q):
        raise RuntimeError("PERMISSION_DENIED: no SELECT on system.compute.clusters")
    r = _call(broken, issue="no_autotermination", resource_key="c1", cluster_id="c_ap")
    t = r.content[0].text
    assert t.startswith("# warning: could not check") and "+      autotermination_minutes: 20" in t


def test_without_cluster_id_behaviour_unchanged():
    r = _call(make_db(), issue="no_autotermination", resource_key="c1")
    assert r.content[0].text.startswith("--- a/databricks.yml")
