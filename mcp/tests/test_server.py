import asyncio
import yaml
import pytest

from dbu_mcp import queries, fixes, _vendored
from dbu_mcp.server import build

MOCK = {
    "billing_origin_product, ROUND": [{"billing_origin_product": "JOBS", "list_cost_usd": 700.0},
                                       {"billing_origin_product": "ALL_PURPOSE", "list_cost_usd": 300.0}],
    "SELECT usage_date, ROUND": [{"usage_date": "2026-10-01", "list_cost_usd": 33.3}],
    "AS resource_type": [{"resource_type": "job", "resource_id": "123", "list_cost_usd": 500.0}],
    "jobs_using_cluster": [{"cluster_id": "c1", "jobs_using_cluster": 2, "cluster_all_purpose_cost_usd": 300.0,
                            "est_saving_if_job_compute_usd": 165.0}],
    "auto_termination_minutes DESC": [{"cluster_id": "c2", "auto_termination_minutes": 0}],
    "avg_cpu_pct": [{"cluster_id": "c3", "est_saving_usd": 35.0}],
    "delta_usd": [{"resource_id": "123", "day_cost_usd": 90.0, "baseline_daily_usd": 30.0, "delta_usd": 60.0},
                  {"resource_id": "w1", "day_cost_usd": 5.0, "baseline_daily_usd": 10.0, "delta_usd": -5.0}],
}
seen = []


def runner(q):
    seen.append(q)
    for marker, rows in MOCK.items():
        if marker in q:
            return rows
    raise AssertionError("unexpected query:\n" + q)


def call(srv, name, **args):
    res = asyncio.run(srv.call_tool(name, args))
    assert not res.is_error, res.content
    sc = res.structured_content
    if isinstance(sc, dict):
        return sc.get("result", sc)
    import json
    txt = res.content[0].text
    try:
        return json.loads(txt)
    except ValueError:
        return txt


@pytest.fixture
def srv():
    seen.clear()
    return build(runner)


def test_tools_listed(srv):
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names == {"cost_summary", "top_cost_drivers", "find_waste", "explain_cost_spike", "propose_fix"}


def test_cost_summary(srv):
    r = call(srv, "cost_summary", days=30)
    assert r["total_list_cost_usd"] == 1000.0 and r["daily"][0]["list_cost_usd"] == 33.3


def test_top_cost_drivers(srv):
    assert call(srv, "top_cost_drivers")[0]["resource_id"] == "123"


def test_find_waste_saving_scaled_to_month(srv):
    r = call(srv, "find_waste", days=15)
    assert r["est_monthly_saving_usd"] == 400.0  # (165+35)*30/15
    assert r["missing_autotermination"][0]["cluster_id"] == "c2"
    assert "errors" not in r


def test_find_waste_degrades_when_schema_missing():
    def r2(q):
        if "node_timeline" in q:
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND system.compute.node_timeline")
        return runner(q)
    r = call(build(r2), "find_waste")
    assert "idle_or_oversized" in r["errors"] and r["est_monthly_saving_usd"] == 165.0


def test_explain_cost_spike(srv):
    r = call(srv, "explain_cost_spike", date="2026-10-20")
    assert r["net_delta_top15_usd"] == 55.0
    assert "DATE'2026-10-20'" in seen[-1]


def test_spike_rejects_injection():
    with pytest.raises(ValueError):
        queries.render_spike("2026-10-20'; DROP TABLE x; --")


def test_all_queries_read_only(srv):
    call(srv, "cost_summary"); call(srv, "find_waste"); call(srv, "explain_cost_spike", date="2026-10-01")
    for q in seen:
        up = q.upper()
        for kw in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "MERGE ", "ALTER ", "CREATE "):
            assert kw not in up
        assert "system." in q


def test_queries_reused_from_notebook():
    assert queries.PRICED_USAGE == _vendored.PRICED_USAGE
    assert queries.WASTE == _vendored.WASTE and queries.COST_BY_PRODUCT == _vendored.COST_BY_PRODUCT


BUNDLE = """
bundle: {name: demo}
resources:
  jobs:
    nightly_etl:
      name: nightly-etl
      tasks:
        - task_key: ingest
          existing_cluster_id: 0101-abc
          notebook_task: {notebook_path: ./ingest.py}
  clusters:
    shared_dev:
      cluster_name: shared-dev
      spark_version: 15.4.x-scala2.12
      node_type_id: i3.xlarge
      num_workers: 2
"""


def test_propose_fix_all_purpose(srv):
    d = call(srv, "propose_fix", bundle_yaml=BUNDLE, issue="all_purpose_job", resource_key="nightly_etl",
             node_type_id="i3.xlarge")
    minus = [l.strip() for l in d.splitlines() if l.startswith("-") and not l.startswith("---")]
    plus = [l[1:].strip() for l in d.splitlines() if l.startswith("+") and not l.startswith("+++")]
    assert any("existing_cluster_id: 0101-abc" in m for m in minus)
    assert "job_cluster_key: nightly_etl_job_cluster" in plus and "node_type_id: i3.xlarge" in plus
    assert d.startswith("--- a/databricks.yml")


def test_propose_fix_autotermination_and_result_is_valid_yaml():
    d = fixes.propose(BUNDLE, "no_autotermination", "shared_dev", minutes=20)
    assert "+      autotermination_minutes: 20" in d
    new = fixes.fix_autotermination(yaml.safe_load(BUNDLE), "shared_dev", 20)
    assert yaml.safe_load(fixes._dump(new))["resources"]["clusters"]["shared_dev"]["autotermination_minutes"] == 20


def test_propose_fix_errors():
    with pytest.raises(KeyError):
        fixes.propose(BUNDLE, "all_purpose_job", "missing")
    with pytest.raises(ValueError):
        fixes.propose(BUNDLE, "bogus", "nightly_etl")
