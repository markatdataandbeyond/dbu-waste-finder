import json, io
from unittest import mock
from dbu_mcp.stmt_api import statement_runner


def _resp(d):
    m = mock.MagicMock(); m.__enter__.return_value = io.StringIO(json.dumps(d)); return m


def test_statement_runner_polls_and_converts():
    first = {"statement_id": "s1", "status": {"state": "PENDING"}}
    done = {"statement_id": "s1", "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": "p", "type_name": "STRING"},
                                                {"name": "c", "type_name": "DOUBLE"},
                                                {"name": "n", "type_name": "LONG"}]}},
            "result": {"data_array": [["ALL_PURPOSE", "82.5", "3"], [None, None, None]]}}
    with mock.patch("urllib.request.urlopen", side_effect=[_resp(first), _resp(done)]), \
         mock.patch("time.sleep"):
        rows = statement_runner("h", "t", "w")("SELECT 1")
    assert rows == [{"p": "ALL_PURPOSE", "c": 82.5, "n": 3}, {"p": None, "c": None, "n": None}]


def test_statement_runner_empty_result_and_error():
    empty = {"statement_id": "s", "status": {"state": "SUCCEEDED"},
             "manifest": {"schema": {"columns": [{"name": "x", "type_name": "INT"}]}}, "result": {}}
    with mock.patch("urllib.request.urlopen", return_value=_resp(empty)):
        assert statement_runner("h", "t", "w")("q") == []
    bad = {"statement_id": "s", "status": {"state": "FAILED", "error": {"message": "TABLE_OR_VIEW_NOT_FOUND"}}}
    with mock.patch("urllib.request.urlopen", return_value=_resp(bad)):
        try:
            statement_runner("h", "t", "w")("q"); assert False
        except RuntimeError as e:
            assert "TABLE_OR_VIEW_NOT_FOUND" in str(e)


def test_server_picks_runner_by_env(monkeypatch):
    import sys, types
    from dbu_mcp import server
    monkeypatch.setenv("DATABRICKS_HOST", "h"); monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "w")
    with mock.patch("dbu_mcp.stmt_api.statement_runner", return_value="STMT") as sr:
        assert server.databricks_runner() == "STMT"; sr.assert_called_once()
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID"); monkeypatch.setenv("DATABRICKS_HTTP_PATH", "/p")
    fake = types.ModuleType("databricks"); fake.sql = types.SimpleNamespace(connect=None)
    monkeypatch.setitem(sys.modules, "databricks", fake)
    with mock.patch("dbu_mcp.stmt_api.statement_runner") as sr:
        assert callable(server.databricks_runner()); sr.assert_not_called()
