"""Report data assembly and charts, on the real engine data."""

import matplotlib.pyplot as plt
import pytest

from saas_valuation.config import load_config, project_path

CFG = load_config()
pytestmark = pytest.mark.skipif(not (project_path(CFG["data"]["processed_dir"]) / "comps.csv").exists(),
                                reason="run the pipeline and comps first")


@pytest.fixture(scope="module")
def universe():
    from saas_valuation.research.data import load_universe
    return load_universe()


def test_company_report_matches_the_models(universe):
    c = universe.company("ADBE")
    assert c.target["price_target"] == universe.targets.at["ADBE", "price_target"]
    assert c.dcf["implied_price"] == universe.dcf_summary.at["ADBE", "implied_price"]
    lo, hi = c.week52
    assert 0 < lo <= c.prices["close"].iloc[-1] * 1.5 and hi >= lo


def test_fiscal_fcf_uses_the_comps_definition(universe):
    f = universe.company("HUBS").fiscal
    row = f.iloc[-1]
    expected = row["operating_cash_flow"] - row["capex"] - row["capitalized_software"]
    assert row["fcf"] == pytest.approx(expected)
    assert row["fcf_after_sbc"] == pytest.approx(expected - row["stock_based_comp"])


def test_every_chart_draws_for_every_company(universe, tmp_path):
    from saas_valuation.research.charts import CHARTS, SOURCES, draw_all
    assert set(CHARTS) == set(SOURCES)                 # every chart has a source caption
    for tk in universe.comps.index:
        paths = draw_all(universe.company(tk), tmp_path / tk)
        assert len(paths) == len(CHARTS) and all(p.stat().st_size > 2000 for p in paths)
    svg = (tmp_path / "ADBE" / "price.svg").read_text()
    assert "<text" in svg and "Price target" in svg     # text kept as text, so the PDF uses the page font
    assert not plt.get_fignums()                        # every figure closed
