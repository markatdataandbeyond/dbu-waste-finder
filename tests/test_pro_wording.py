"""Pro wording must be 'Pro, waitlist' + email everywhere it appears."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["docs/index.html", "README.md", "mcp/README.md", "notebook/dbu_waste_finder.py"]


def _t(p):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", (ROOT / p).read_text()))


def test_no_old_pro_wording_anywhere():
    for f in FILES:
        t = _t(f).lower()
        assert "pro is planned" not in t, f
        assert "email me" not in t, f


def test_every_pro_mention_says_waitlist_and_email():
    for f in FILES:
        t = _t(f)
        if re.search(r"\bPro\b", t):
            assert "Pro, waitlist" in t, f
            assert "mark@dataandbeyond.dk" in t, f


def test_page_and_notebook_both_mention_pro():
    assert "Pro, waitlist" in _t("docs/index.html")
    assert "Pro, waitlist" in _t("notebook/dbu_waste_finder.py")
