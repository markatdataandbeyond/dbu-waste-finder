"""Edge cases for the three classic-cluster checks, with fixture rows shaped like system.compute.* (docs, Oct 2026)."""
import runpy
import pytest
from test_notebook import make_db, NB, _f

pytest.importorskip("duckdb")

EXTRA_CLUSTERS = """INSERT INTO compute_clusters VALUES
 ('w1','c_del','deleted-in-window','d@x','UI', 0, current_timestamp-INTERVAL 4 DAY, (current_timestamp-INTERVAL 2 DAY)::TIMESTAMP),
 ('w1','c_olddel','deleted-long-ago','d@x','UI', 0, current_timestamp-INTERVAL 70 DAY, (current_timestamp-INTERVAL 60 DAY)::TIMESTAMP),
 ('w1','c_null','api-null','e@x','API', NULL, current_timestamp-INTERVAL 3 DAY, NULL::TIMESTAMP),
 ('w1','c_60','exactly-60','f@x','UI', 60, current_timestamp-INTERVAL 3 DAY, NULL::TIMESTAMP),
 ('w1','c_61','sixty-one','f@x','UI', 61, current_timestamp-INTERVAL 3 DAY, NULL::TIMESTAMP),
 ('w1','c_jobzero','job-cluster-0','svc','JOB', 0, current_timestamp-INTERVAL 3 DAY, NULL::TIMESTAMP),
 ('w1','c_pipe','dlt','svc','PIPELINE', 0, current_timestamp-INTERVAL 3 DAY, NULL::TIMESTAMP)"""
# job cluster c_job (200 DBU x $0.15 = $30 in window) running at 2 % CPU: oversized job compute
EXTRA_NODES = """INSERT INTO compute_node_timeline VALUES
 ('c_job', current_timestamp-INTERVAL 3 DAY, 1.5, 0.5, 15.0)"""


def _res():
    nb = runpy.run_path(NB)
    return nb["run_all"](make_db(extra=[EXTRA_CLUSTERS, EXTRA_NODES]))


def test_autotermination_edge_cases():
    res = _res()
    got = {r["cluster_id"]: r for r in res["missing_autotermination"]}
    # 0 / NULL / >60 on UI+API; exactly 60 not flagged; JOB and PIPELINE ignored even with 0
    assert {"c_ap", "c_null", "c_61"} <= set(got)
    assert not {"c_60", "c_jobzero", "c_pipe", "c_job"} & set(got)


def test_cluster_deleted_inside_window_is_still_reported():
    got = {r["cluster_id"]: r for r in _res()["missing_autotermination"]}
    assert "c_del" in got and got["c_del"]["delete_time"] is not None   # burned DBUs before deletion
    assert "c_olddel" not in got                                       # deleted before the window
    assert got["c_ap"]["delete_time"] is None


def test_idle_job_cluster_flagged_and_cluster_without_node_rows_ignored():
    idle = {r["cluster_id"]: r for r in _res()["idle_or_oversized"]}
    assert _f(idle["c_job"]["est_saving_usd"]) == 15.0 and _f(idle["c_job"]["avg_cpu_pct"]) == 2.0
    # c_ap is busy (70 % CPU); c_null has no node_timeline rows (<10 min nodes may be missing) -> not flagged
    assert "c_ap" not in idle and "c_null" not in idle
