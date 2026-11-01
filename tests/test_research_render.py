"""The report HTML and PDF."""

import json

import numpy as np
import pytest

from saas_valuation.config import load_config, project_path
from saas_valuation.research.data import finite
from saas_valuation.research.render import Fmt, clean_narrative, model_name

CFG = load_config()
NARRATIVE = project_path(CFG["data"]["processed_dir"]) / "research" / "ADBE.json"


def test_finite_accepts_ints_and_rejects_missing():
    assert finite(97_400_000_000) and finite(np.int64(5)) and finite(np.float64(1.5))
    assert not finite(None) and not finite(float("nan")) and not finite(True) and not finite("3")


def test_formatting():
    f = Fmt()
    assert f.money(388.0) == "$388" and f.price(250.234) == "$250.23" and f.big(97.4e9) == "$97.4B"
    assert f.millions(-12_300_000) == "(12)" and f.pct(0.551, signed=True) == "+55%" and f.mult(np.nan) == "NM"
    assert model_name("claude-opus-5-5") == "Claude Opus 5.5"


def test_fact_tags_never_reach_readers():
    n = {"headline": "H", "summary": "Target $388 [F2].", "business_overview": "B", "valuation": "V [F9]",
         "thesis": [{"title": "T [F1]", "text": "x [F3]"}], "risks": [{"title": "R", "text": "y [F4]"}],
         "what_would_change": ["z [F5]"]}
    assert "[F" not in json.dumps(clean_narrative(n))


@pytest.fixture(scope="module")
def adbe_html(tmp_path_factory):
    if not NARRATIVE.exists() or json.loads(NARRATIVE.read_text()).get("status") != "ok":
        pytest.skip("no checked narrative for ADBE (run research.narrative)")
    from saas_valuation.research.charts import draw_all
    from saas_valuation.research.data import load_universe
    from saas_valuation.research.render import build_html, narrative_for
    u = load_universe()
    c = u.company("ADBE")
    charts = tmp_path_factory.mktemp("charts")
    draw_all(c, charts)
    return c, build_html(u, c, narrative_for(u, "ADBE"), charts)


def test_html_has_every_section_and_no_tags(adbe_html):
    c, html = adbe_html
    for text in ("Business overview", "Financial summary", "Valuation and price target", "Risks to our rating",
                 "What would change our view", "Methodology", "Disclosures", c.target["rating"]):
        assert text in html, text
    assert html.count("<svg") == 6 and "[F" not in html
    assert '"Helvetica Neue"' in html and "&#34;Helvetica" not in html     # CSS not escaped: fonts apply
    assert ">n/a<" not in html.split("Key data")[1].split("</table>")[0]    # every key-data figure present


def test_pdf_prints(adbe_html, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    from saas_valuation.research.render import print_pdf
    c, html = adbe_html
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception as exc:
            pytest.skip(f"Google Chrome unavailable: {exc}")
        out = tmp_path / "r.pdf"
        print_pdf(html, out, c, browser)
        browser.close()
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 50_000          # a real, chart-filled PDF
