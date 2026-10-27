"""Turn a finding into a concrete diff for a Databricks Asset Bundle (DAB) YAML.

DAB schema: resources.jobs.<key>.tasks[].existing_cluster_id / job_cluster_key,
resources.jobs.<key>.job_clusters[].{job_cluster_key,new_cluster};
resources.clusters.<key>.autotermination_minutes (Clusters API field);
resources.sql_warehouses.<key>.auto_stop_mins (SQL Warehouses API field).
Nothing is applied; we only return a unified diff for a human to review/commit.
"""
import copy
import difflib

import yaml


def _dump(doc) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _diff(before: str, after: str, path: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))


def fix_all_purpose_job(doc: dict, job_key: str, spark_version: str, node_type_id: str,
                        num_workers: int = 2) -> dict:
    jobs = (doc.get("resources") or {}).get("jobs") or {}
    if job_key not in jobs:
        raise KeyError(f"job '{job_key}' not found under resources.jobs")
    new = copy.deepcopy(doc)
    job = new["resources"]["jobs"][job_key]
    jc_key = f"{job_key}_job_cluster"
    changed = False
    for t in job.get("tasks") or []:
        if "existing_cluster_id" in t:
            del t["existing_cluster_id"]
            t["job_cluster_key"] = jc_key
            changed = True
    if not changed:
        raise ValueError(f"job '{job_key}' has no tasks with existing_cluster_id")
    clusters = job.setdefault("job_clusters", [])
    if not any(c.get("job_cluster_key") == jc_key for c in clusters):
        clusters.append({"job_cluster_key": jc_key, "new_cluster": {
            "spark_version": spark_version, "node_type_id": node_type_id,
            "num_workers": int(num_workers)}})
    return new


def fix_autotermination(doc: dict, cluster_key: str, minutes: int = 20) -> dict:
    clusters = (doc.get("resources") or {}).get("clusters") or {}
    if cluster_key not in clusters:
        raise KeyError(f"cluster '{cluster_key}' not found under resources.clusters")
    if not 10 <= int(minutes) <= 10000:
        raise ValueError("autotermination_minutes must be 10..10000")
    new = copy.deepcopy(doc)
    new["resources"]["clusters"][cluster_key]["autotermination_minutes"] = int(minutes)
    return new


def fix_warehouse_autostop(doc: dict, warehouse_key: str, minutes: int = 10) -> dict:
    whs = (doc.get("resources") or {}).get("sql_warehouses") or {}
    if warehouse_key not in whs:
        raise KeyError(f"warehouse '{warehouse_key}' not found under resources.sql_warehouses")
    if not 1 <= int(minutes) <= 60:  # serverless allows >=1 min (UI min 5); keep it short
        raise ValueError("auto_stop_mins must be 1..60")
    new = copy.deepcopy(doc)
    new["resources"]["sql_warehouses"][warehouse_key]["auto_stop_mins"] = int(minutes)
    return new


def propose(bundle_yaml: str, issue: str, resource_key: str, path: str = "databricks.yml",
            **kw) -> str:
    doc = yaml.safe_load(bundle_yaml) or {}
    before = _dump(doc)  # normalise first so the diff only shows the real change
    if issue == "all_purpose_job":
        after = fix_all_purpose_job(doc, resource_key, kw.get("spark_version") or "15.4.x-scala2.12",
                                    kw.get("node_type_id") or "<same-node-type-as-today>",
                                    kw.get("num_workers") or 2)
    elif issue == "no_autotermination":
        after = fix_autotermination(doc, resource_key, kw.get("minutes") or 20)
    elif issue == "warehouse_autostop":
        after = fix_warehouse_autostop(doc, resource_key, kw.get("minutes") or 10)
    else:
        raise ValueError(f"issue must be 'all_purpose_job', 'no_autotermination' or 'warehouse_autostop', got {issue!r}")
    return _diff(before, _dump(after), path)
