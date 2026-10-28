"""Delta Lake maintenance waste: detect OPTIMIZE / VACUUM needs and over-frequent OPTIMIZE.

All outputs are *proposals* (SQL text or findings). Nothing is executed.
Thresholds match common Databricks guidance (target file ~128 MiB; vacuum retain >= 168 h).
"""
from __future__ import annotations

import re
from typing import Any

TARGET_FILE_BYTES = 128 * 1024 * 1024  # 128 MiB
MIN_FILES_FOR_OPTIMIZE = 50
MAX_AVG_FILE_RATIO = 0.25  # avg size << target => fragmented
VACUUM_STALE_DAYS = 14
MIN_VACUUMABLE_BYTES = 1 * 1024**3  # 1 GiB reclaimable
MIN_RETENTION_HOURS = 168  # Delta / Databricks safety floor for VACUUM
MAX_OPTIMIZE_PER_WEEK = 3.0

_TABLE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){1,2}$"
)
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ISSUES = (
    "delta_optimize",
    "delta_vacuum",
    "delta_zorder",
    "delta_predictive_optimization",
    "delta_overfrequent_optimize",
)


def _f(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def needs_optimize(metrics: dict[str, Any], *, min_files: int = MIN_FILES_FOR_OPTIMIZE,
                   target_file_bytes: int = TARGET_FILE_BYTES,
                   max_avg_ratio: float = MAX_AVG_FILE_RATIO) -> bool:
    """True when file_count is high and average file size is well below the target."""
    n = _f(metrics.get("file_count"))
    size = _f(metrics.get("size_bytes"))
    if n is None or size is None or n < min_files or size <= 0:
        return False
    return (size / n) < target_file_bytes * max_avg_ratio


def needs_vacuum(metrics: dict[str, Any], *, stale_days: int = VACUUM_STALE_DAYS,
                 min_vacuumable_bytes: int = MIN_VACUUMABLE_BYTES) -> bool:
    """True when vacuum is stale/missing, or reclaimable history is large."""
    vac_bytes = _f(metrics.get("vacuumable_bytes")) or 0.0
    if vac_bytes >= min_vacuumable_bytes:
        return True
    if "days_since_vacuum" not in metrics:
        return False
    days = metrics.get("days_since_vacuum")
    if days is None:  # never vacuumed / unknown last run
        return True
    d = _f(days)
    return d is not None and d >= stale_days


def is_overfrequent_optimize(optimize_runs: int | float | None, window_days: int | float | None,
                             *, max_per_week: float = MAX_OPTIMIZE_PER_WEEK) -> bool:
    runs = _f(optimize_runs)
    days = _f(window_days)
    if runs is None or days is None or days <= 0 or runs <= 0:
        return False
    return (runs * 7.0 / days) > max_per_week


def assess_table(metrics: dict[str, Any], **thresholds) -> list[dict[str, Any]]:
    """Return finding dicts for a single table's fixture / DESCRIBE DETAIL-style metrics."""
    table = metrics.get("table_fqn") or metrics.get("table") or ""
    out: list[dict[str, Any]] = []
    opt_kw = {k: thresholds[k] for k in ("min_files", "target_file_bytes", "max_avg_ratio") if k in thresholds}
    vac_kw = {k: thresholds[k] for k in ("stale_days", "min_vacuumable_bytes") if k in thresholds}
    of_kw = {k: thresholds[k] for k in ("max_per_week",) if k in thresholds}
    if needs_optimize(metrics, **opt_kw):
        n, size = _f(metrics.get("file_count")), _f(metrics.get("size_bytes"))
        avg = (size / n) if n and size else None
        out.append({"issue": "delta_optimize", "table_fqn": table,
                    "file_count": n, "avg_file_bytes": avg,
                    "reason": "many small files (avg << target)"})
    if needs_vacuum(metrics, **vac_kw):
        out.append({"issue": "delta_vacuum", "table_fqn": table,
                    "days_since_vacuum": metrics.get("days_since_vacuum"),
                    "vacuumable_bytes": metrics.get("vacuumable_bytes"),
                    "reason": "stale/missing VACUUM or large reclaimable history"})
    window = metrics.get("window_days")
    if is_overfrequent_optimize(metrics.get("optimize_runs"), window, **of_kw):
        out.append({"issue": "delta_overfrequent_optimize", "table_fqn": table,
                    "optimize_runs": metrics.get("optimize_runs"), "window_days": window,
                    "reason": "OPTIMIZE runs more often than the weekly budget"})
    return out


def validate_table_fqn(table_fqn: str) -> str:
    name = (table_fqn or "").strip().replace("`", "")
    if not _TABLE_RE.fullmatch(name):
        raise ValueError(
            f"table must be schema.table or catalog.schema.table "
            f"(letters, digits, underscore only), got {table_fqn!r}"
        )
    return name


def validate_columns(cols: list[str]) -> list[str]:
    if not cols:
        raise ValueError("zorder_columns must be a non-empty list of identifiers")
    out = []
    for c in cols:
        c = (c or "").strip().replace("`", "")
        if not _IDENT_RE.fullmatch(c):
            raise ValueError(f"invalid column identifier for ZORDER: {c!r}")
        out.append(c)
    return out


def propose_sql(issue: str, table_fqn: str, *, zorder_columns: list[str] | None = None,
                retention_hours: int = MIN_RETENTION_HOURS) -> str:
    """Return reviewable SQL. Never runs it. VACUUM proposals start with DRY RUN."""
    if issue not in ISSUES:
        raise ValueError(f"issue must be one of {ISSUES}, got {issue!r}")
    table = validate_table_fqn(table_fqn)
    hdr = (f"-- Proposal only; review before running. DBU Waste Finder never executes this.\n"
           f"-- Table: {table}\n")
    if issue == "delta_optimize":
        return hdr + f"OPTIMIZE {table};\n"
    if issue == "delta_zorder":
        cols = validate_columns(list(zorder_columns or []))
        return hdr + f"OPTIMIZE {table} ZORDER BY ({', '.join(cols)});\n"
    if issue == "delta_vacuum":
        h = int(retention_hours)
        if h < MIN_RETENTION_HOURS:
            raise ValueError(f"retention_hours must be >= {MIN_RETENTION_HOURS} (Delta safety floor), got {h}")
        return (hdr
                + f"-- Step 1: dry run (lists files that would be deleted; no changes)\n"
                + f"VACUUM {table} RETAIN {h} HOURS DRY RUN;\n"
                + f"-- Step 2: after reviewing the dry-run output, run without DRY RUN:\n"
                + f"-- VACUUM {table} RETAIN {h} HOURS;\n")
    if issue == "delta_predictive_optimization":
        return (hdr
                + f"-- Enable Databricks predictive optimization so OPTIMIZE/VACUUM run automatically.\n"
                + f"ALTER TABLE {table} ENABLE PREDICTIVE OPTIMIZATION;\n")
    return (hdr
            + f"-- OPTIMIZE is running too often on {table}. Prefer fewer scheduled runs "
            + f"or predictive optimization.\n"
            + f"-- 1) Reduce the job schedule that runs OPTIMIZE on this table\n"
            + f"-- 2) Or enable predictive optimization and drop the manual OPTIMIZE job:\n"
            + f"ALTER TABLE {table} ENABLE PREDICTIVE OPTIMIZATION;\n")


# Spark/Databricks SQL. Placeholders: {d}, {min_files}, {target_file_bytes}, {max_avg_ratio},
# {vacuum_stale_days}, {opt_max_per_week}.

DELTA_NEEDS_OPTIMIZE = """
WITH comp AS (
  SELECT catalog_name, schema_name, table_name,
         SUM(CAST(operation_metrics['number_of_compacted_files'] AS DOUBLE)) AS files_in,
         SUM(CAST(operation_metrics['amount_of_data_compacted_bytes'] AS DOUBLE)) AS bytes_in,
         SUM(CAST(operation_metrics['number_of_output_files'] AS DOUBLE)) AS files_out,
         SUM(usage_quantity) AS est_dbus,
         COUNT(*) AS compaction_ops,
         MAX(end_time) AS last_optimize
  FROM system.storage.predictive_optimization_operations_history
  WHERE operation_type = 'COMPACTION'
    AND operation_status = 'SUCCESSFUL'
    AND start_time >= date_add(current_date(), -{d})
  GROUP BY catalog_name, schema_name, table_name
)
SELECT concat_ws('.', catalog_name, schema_name, table_name) AS table_fqn,
       CAST(files_in AS BIGINT) AS file_count,
       ROUND(bytes_in / NULLIF(files_in, 0), 0) AS avg_input_file_bytes,
       CAST(files_out AS BIGINT) AS files_out,
       compaction_ops, last_optimize,
       ROUND(est_dbus, 3) AS est_dbus
FROM comp
WHERE files_in >= {min_files}
  AND bytes_in / NULLIF(files_in, 0) < {target_file_bytes} * {max_avg_ratio}
ORDER BY files_in DESC"""

DELTA_NEEDS_VACUUM = """
WITH vac AS (
  SELECT catalog_name, schema_name, table_name,
         MAX(end_time) AS last_vacuum,
         SUM(CAST(operation_metrics['amount_of_data_deleted_bytes'] AS DOUBLE)) AS bytes_deleted,
         SUM(CAST(operation_metrics['number_of_deleted_files'] AS DOUBLE)) AS files_deleted
  FROM system.storage.predictive_optimization_operations_history
  WHERE operation_type = 'VACUUM'
    AND start_time >= date_add(current_date(), -{d})
  GROUP BY catalog_name, schema_name, table_name
), touched AS (
  SELECT DISTINCT catalog_name, schema_name, table_name
  FROM system.storage.predictive_optimization_operations_history
  WHERE start_time >= date_add(current_date(), -{d})
)
SELECT concat_ws('.', t.catalog_name, t.schema_name, t.table_name) AS table_fqn,
       v.last_vacuum,
       CASE WHEN v.last_vacuum IS NULL THEN NULL
            ELSE ROUND(TIMESTAMPDIFF(DAY, v.last_vacuum, current_timestamp()), 1) END AS days_since_vacuum,
       v.bytes_deleted, v.files_deleted
FROM touched t
LEFT JOIN vac v
  ON t.catalog_name = v.catalog_name AND t.schema_name = v.schema_name AND t.table_name = v.table_name
WHERE v.last_vacuum IS NULL
   OR TIMESTAMPDIFF(DAY, v.last_vacuum, current_timestamp()) > {vacuum_stale_days}
ORDER BY days_since_vacuum DESC NULLS FIRST"""

DELTA_OVERFREQUENT_OPTIMIZE = """
WITH raw AS (
  SELECT statement_text, start_time
  FROM system.query.history
  WHERE start_time >= date_add(current_date(), -{d})
    AND upper(ltrim(statement_text)) LIKE 'OPTIMIZE %'
), parsed AS (
  SELECT start_time,
         lower(regexp_replace(
           regexp_extract(upper(statement_text), 'OPTIMIZE[ \\\\t]+([A-Z0-9_`\\\\.]+)', 1),
           '`', '')) AS table_fqn
  FROM raw
), agg AS (
  SELECT table_fqn, COUNT(*) AS optimize_runs,
         ROUND(COUNT(*) * 7.0 / {d}, 2) AS runs_per_week
  FROM parsed
  WHERE table_fqn IS NOT NULL AND table_fqn <> ''
  GROUP BY table_fqn
)
SELECT a.table_fqn, a.optimize_runs, a.runs_per_week,
       CAST(NULL AS STRING) AS job_id,
       CAST(NULL AS DOUBLE) AS list_cost_usd
FROM agg a
WHERE a.runs_per_week > {opt_max_per_week}
ORDER BY a.optimize_runs DESC"""
