"""Risk b) a shared all-purpose cluster: only the share of its billed time used by job runs counts as saving;
the full figure is kept as an explicit upper bound."""
import runpy
import pytest
from test_notebook import make_db, NB, _f

pytest.importorskip("duckdb")

RATIO = 1 - 0.15 / 0.55   # AWS PREMIUM jobs vs all-purpose
# c_ap is billed for 1 hour ($55). Replace the task timeline: j1 ran 15 min on it.
def timeline(*rows):
    vals = ",".join(f"('w1','{j}', current_timestamp-INTERVAL 1 DAY, current_timestamp-INTERVAL 1 DAY + INTERVAL {m} MINUTE, ['{c}'])"
                    for j, c, m in rows)
    return ["DROP TABLE lakeflow_job_task_run_timeline",
            f"CREATE TABLE lakeflow_job_task_run_timeline AS SELECT * FROM (VALUES {vals}) "
            "t(workspace_id,job_id,period_start_time,period_end_time,compute_ids)"]


def _ap(rows):
    return {r["cluster_id"]: r for r in runpy.run_path(NB)["run_all"](make_db(extra=timeline(*rows)))["jobs_on_all_purpose"]}


def test_shared_cluster_counts_only_job_share():
    r = _ap([("j1", "c_ap", 15), ("j2", "c_job", 30)])["c_ap"]
    assert abs(_f(r["job_share"]) - 0.25) < 0.001
    assert abs(_f(r["est_saving_if_job_compute_usd"]) - round(55 * 0.25 * RATIO, 2)) < 0.011   # 10.0
    assert abs(_f(r["est_saving_upper_bound_usd"]) - round(55 * RATIO, 2)) < 0.011              # 40.0
    assert r["saving_basis"] == "job_share"


def test_parallel_tasks_share_capped_at_one():
    r = _ap([("j1", "c_ap", 50), ("j3", "c_ap", 50)])["c_ap"]   # 100 task-minutes on a 60-minute cluster
    assert _f(r["job_share"]) == 1.0
    assert abs(_f(r["est_saving_if_job_compute_usd"]) - round(55 * RATIO, 2)) < 0.011


def test_no_billed_time_falls_back_to_flagged_upper_bound():
    run = make_db(extra=timeline(("j1", "c_ap", 15)) + ["UPDATE billing_usage SET usage_start_time = usage_end_time "
                                                        "WHERE usage_metadata.cluster_id = 'c_ap'"])
    r = {x["cluster_id"]: x for x in runpy.run_path(NB)["run_all"](run)["jobs_on_all_purpose"]}["c_ap"]
    assert r["job_share"] is None and r["saving_basis"] == "upper_bound"
    assert _f(r["est_saving_if_job_compute_usd"]) == _f(r["est_saving_upper_bound_usd"])
