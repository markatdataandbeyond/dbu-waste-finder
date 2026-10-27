"""Risk a) jobs_on_all_purpose must price with the cluster's own SKU + cloud, not an average over all clouds/tiers."""
import runpy
import pytest
from test_notebook import make_db, NB, _f

pytest.importorskip("duckdb")

# Other clouds/tiers with very different prices. The old AVG()-over-everything ratio would be
# (0.15+0.30+0.20)/3 / ((0.55+0.40+0.65)/3) = 0.406 instead of the correct AWS PREMIUM 0.15/0.55 = 0.273.
SKEW = """INSERT INTO billing_list_prices VALUES
 ('PREMIUM_ALL_PURPOSE_COMPUTE','AZURE','DBU', TIMESTAMP '2020-01-01', NULL, {'default':'0.40','effective_list':{'default':'0.40'}}, 'USD'),
 ('PREMIUM_JOBS_COMPUTE','AZURE','DBU', TIMESTAMP '2020-01-01', NULL, {'default':'0.30','effective_list':{'default':'0.30'}}, 'USD'),
 ('ENTERPRISE_ALL_PURPOSE_COMPUTE','AWS','DBU', TIMESTAMP '2020-01-01', NULL, {'default':'0.65','effective_list':{'default':'0.65'}}, 'USD'),
 ('ENTERPRISE_JOBS_COMPUTE','AWS','DBU', TIMESTAMP '2020-01-01', NULL, {'default':'0.20','effective_list':{'default':'0.20'}}, 'USD')"""


def _ap(extra=()):
    return {r["cluster_id"]: r for r in runpy.run_path(NB)["run_all"](make_db(extra=[SKEW, *extra]))["jobs_on_all_purpose"]}


def test_ratio_uses_cluster_sku_and_cloud():
    r = _ap()["c_ap"]
    assert r["jobs_sku"] == "PREMIUM_JOBS_COMPUTE" and r["cloud"] == "AWS"
    assert abs(_f(r["jobs_to_all_purpose_price_ratio"]) - 0.15 / 0.55) < 0.001
    share = _f(r.get("job_share")) if r.get("job_share") is not None else 1.0
    assert abs(_f(r["est_saving_if_job_compute_usd"]) - round(55 * share * (1 - 0.15 / 0.55), 2)) < 0.011


def test_photon_sku_maps_to_photon_jobs_sku():
    extra = ["""INSERT INTO billing_list_prices VALUES
     ('PREMIUM_ALL_PURPOSE_COMPUTE_(PHOTON)','AWS','DBU', TIMESTAMP '2020-01-01', NULL, {'default':'0.55','effective_list':{'default':'0.55'}}, 'USD'),
     ('PREMIUM_JOBS_COMPUTE_(PHOTON)','AWS','DBU', TIMESTAMP '2020-01-01', NULL, {'default':'0.10','effective_list':{'default':'0.10'}}, 'USD')""",
             "UPDATE billing_usage SET sku_name = 'PREMIUM_ALL_PURPOSE_COMPUTE_(PHOTON)' WHERE usage_metadata.cluster_id = 'c_ap'"]
    r = _ap(extra)["c_ap"]
    assert r["jobs_sku"] == "PREMIUM_JOBS_COMPUTE_(PHOTON)"
    assert abs(_f(r["jobs_to_all_purpose_price_ratio"]) - 0.10 / 0.55) < 0.001


def test_missing_jobs_price_gives_unknown_not_average():
    r = _ap(["DELETE FROM billing_list_prices WHERE sku_name = 'PREMIUM_JOBS_COMPUTE' AND cloud = 'AWS'"])["c_ap"]
    assert r["jobs_to_all_purpose_price_ratio"] is None and r["est_saving_if_job_compute_usd"] is None
