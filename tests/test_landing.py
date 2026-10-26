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


PRO = "Pro is planned (around $149/month per workspace). Not built yet \u2013 email me if you'd pay for it."


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
