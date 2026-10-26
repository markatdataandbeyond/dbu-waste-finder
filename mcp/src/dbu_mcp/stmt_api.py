"""Runner using the Databricks SQL Statement Execution API (no connector needed)."""
import json, os, time, urllib.request
from typing import Any


def statement_runner(host: str | None = None, token: str | None = None, warehouse_id: str | None = None,
                     timeout_s: int = 300):
    host = "https://" + (host or os.environ["DATABRICKS_HOST"]).removeprefix("https://").rstrip("/")
    token = token or os.environ["DATABRICKS_TOKEN"]
    wid = warehouse_id or os.environ["DATABRICKS_WAREHOUSE_ID"]

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(host + path, method=method,
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def run(q: str) -> list[dict[str, Any]]:
        r = call("POST", "/api/2.0/sql/statements", {"statement": q, "warehouse_id": wid,
                 "wait_timeout": "50s", "on_wait_timeout": "CONTINUE", "format": "JSON_ARRAY",
                 "disposition": "INLINE"})
        t0 = time.time()
        while r["status"]["state"] in ("PENDING", "RUNNING"):
            if time.time() - t0 > timeout_s:
                raise TimeoutError(q[:80])
            time.sleep(2)
            r = call("GET", f"/api/2.0/sql/statements/{r['statement_id']}")
        if r["status"]["state"] != "SUCCEEDED":
            raise RuntimeError(r["status"].get("error", r["status"]))
        cols = [c["name"] for c in r["manifest"]["schema"]["columns"]]
        types = [c["type_name"] for c in r["manifest"]["schema"]["columns"]]
        rows = []
        for row in (r.get("result") or {}).get("data_array") or []:
            rows.append({c: _conv(v, t) for c, v, t in zip(cols, row, types)})
        return rows
    return run


def _conv(v, t):
    if v is None:
        return None
    if t in ("DOUBLE", "FLOAT", "DECIMAL"):
        return float(v)
    if t in ("INT", "LONG", "SHORT", "BYTE"):
        return int(v)
    return v
