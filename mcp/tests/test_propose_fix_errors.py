"""propose_fix: the AI client must see the real error, not a generic 'Error executing tool'."""
import asyncio
import pytest
from dbu_mcp.server import build

Y = "resources:\n  sql_warehouses:\n    bi:\n      auto_stop_mins: 120\n  clusters:\n    c1:\n      autotermination_minutes: 0\n"


def _err(args):
    srv = build(runner=lambda q: [])
    with pytest.raises(Exception) as ei:
        asyncio.run(srv.call_tool("propose_fix", args))
    return str(ei.value)


@pytest.mark.parametrize("issue,key,expect", [
    ("warehouse_autostop", "missing", "warehouse 'missing' not found"),
    ("no_autotermination", "nope", "nope"),
])
def test_propose_fix_unknown_resource_shows_real_message(issue, key, expect):
    msg = _err({"bundle_yaml": Y, "issue": issue, "resource_key": key})
    assert expect in msg, msg
    assert msg.strip() != "Error executing tool propose_fix"


def test_propose_fix_unknown_issue_shows_message():
    msg = _err({"bundle_yaml": Y, "issue": "bogus", "resource_key": "bi"})
    assert "bogus" in msg, msg


def test_propose_fix_invalid_yaml_shows_message():
    msg = _err({"bundle_yaml": "a: [unclosed", "issue": "warehouse_autostop", "resource_key": "bi"})
    assert msg.strip() != "Error executing tool propose_fix", msg


def test_propose_fix_happy_path_unchanged():
    r = asyncio.run(build(runner=lambda q: []).call_tool(
        "propose_fix", {"bundle_yaml": Y, "issue": "warehouse_autostop", "resource_key": "bi"}))
    txt = r.content[0].text
    assert "+      auto_stop_mins: 10" in txt
