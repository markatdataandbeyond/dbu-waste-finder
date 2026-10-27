"""Landing page + README must not promise things that don't exist, and must use one product name."""
import pathlib, re

ROOT = pathlib.Path(__file__).parents[1]
PAGE = (ROOT / "docs/index.html").read_text()
README = (ROOT / "README.md").read_text()
NAME = "DBU Waste Finder"
FORBIDDEN = ["DRAFT", "not published", "every week", "weekly", "Slack", "alert", "Databricks App",
             "how much you save", "10 minutes", "savings tracking", "DBU Watchdog"]
# checks that exist in notebook/dbu_waste_finder.py and are covered by tests
TESTED_CHECKS = {"Top cost drivers", "Jobs on all-purpose compute", "Cluster auto-termination",
                 "Idle or oversized clusters", "SQL warehouse auto-stop", "Idle SQL warehouses", "Serverless jobs"}


def _text(html):
    return re.sub(r"<[^>]+>", " ", html)


def test_no_draft_or_unbuilt_promises():
    t = _text(PAGE)
    for w in FORBIDDEN:
        assert w.lower() not in t.lower(), f"landing page still says {w!r}"


def test_one_name_everywhere():
    assert NAME in PAGE and NAME in README
    assert "Watchdog" not in PAGE + README


def test_only_tested_checks_listed():
    cards = set(re.findall(r'<div class="card"><b>([^<]+)</b>', PAGE))
    assert cards, "no checks listed"
    assert cards <= TESTED_CHECKS, f"untested/unknown checks on page: {cards - TESTED_CHECKS}"
    assert {"SQL warehouse auto-stop", "Idle SQL warehouses"} <= cards


def test_readme_describes_serverless_checks():
    for k in ("warehouse_long_autostop", "idle_warehouse", "serverless_jobs", "warehouse_autostop"):
        assert k in README + (ROOT / "mcp/README.md").read_text()


PRO = "Pro, waitlist (around $149/month per workspace). Not built yet \u2013 email mark@dataandbeyond.dk to join the waitlist."


def test_pro_section_exact_one_price_no_competitors():
    t = re.sub(r"\s+", " ", _text(PAGE))
    assert PRO in t
    assert 'href="mailto:mark@dataandbeyond.dk' in PAGE
    assert re.findall(r"\$\d[\d,]*", t) == ["$149"]
    for c in ("LakeSentry", "SELECT from", "DataFlint", "$299", "comparison"):
        assert c not in t


def test_privacy_honest_about_mcp():
    t = re.sub(r"\s+", " ", _text(PAGE)).lower()
    assert "no data leaves your workspace" not in t.replace("the notebook: no data leaves your workspace", "")
    assert "mcp server runs on your machine" in t and "ai client" in t
    assert "email addresses" in t
    r = README.lower()
    assert "mcp server runs on your machine" in r and "ai client" in r


SAVINGS_PROMISE = re.compile(r"\b(save|saves|saved|saving|savings)\b|can cut|cut your|reduce your (bill|cost)|lower your (bill|cost)",
                             re.IGNORECASE)


def test_no_savings_promises_on_page_or_readmes():
    for name, txt in (("landing", _text(PAGE)), ("README", README), ("mcp/README", (ROOT / "mcp/README.md").read_text())):
        hits = SAVINGS_PROMISE.findall(txt)
        assert not hits, f"{name} promises savings: {hits}"
    assert "observation_days" in README and "low_confidence" in README  # projection method documented


def test_validation_status_not_stale():
    vs = README.split("## Validation status", 1)[1]
    assert "USD unknown" not in vs
    assert "partial" in vs.lower() and "GENIE_FREE_USAGE" in vs


MCP_README = (ROOT / "mcp/README.md").read_text()
DOCS = {"docs/index.html": _text(PAGE), "README.md": README, "mcp/README.md": MCP_README}


def test_install_lines_use_python3():
    # systems with only python3 (Debian/Ubuntu, macOS) have no `python`
    for name, t in (("README.md", README), ("mcp/README.md", MCP_README)):
        assert not re.search(r"(?<![\w/.])python -m venv", t), f"{name} uses bare `python -m venv`"
        assert "python3 -m venv" in t, f"{name} has no python3 venv install line"


def test_privacy_split_everywhere():
    # every doc must say: notebook -> nothing leaves; MCP -> results go to the AI client and its provider,
    # listing names, ids, amounts and owner emails
    for name, t in DOCS.items():
        low = " ".join(t.split()).lower()
        assert "privacy" in low, f"{name} lacks a Privacy section"
        assert re.search(r"nothing (leaves|is sent anywhere)", low), f"{name}: notebook 'nothing leaves' missing"
        assert "ai client" in low and "provider" in low, f"{name}: MCP -> AI client/provider missing"
        for k in ("names", "ids", "amounts", "email"):
            assert k in low, f"{name}: MCP privacy text does not mention {k!r}"
        assert "nothing is sent to data and beyond" in low or "nothing is sent to us" in low, name
