"""Runs the dashboard headlessly with Streamlit's AppTest.

Needs the processed data from the pipeline, so it's skipped on a fresh clone.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "dashboard" / "app.py")

pytestmark = pytest.mark.skipif(not (ROOT / "data" / "processed" / "comps.csv").exists(),
                                reason="run the pipeline and comps first")


def run_app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def metric(at: AppTest, label: str) -> str:
    return next(m.value for m in at.metric if m.label == label)


def test_dashboard_renders_all_tabs():
    at = run_app()
    assert [t.label for t in at.tabs] == ["Ranking", "Company", "Comps", "Data & method"]
    assert int(metric(at, "Undervalued")) + int(metric(at, "Overvalued")) <= 18


def test_higher_risk_free_rate_lowers_valuations():
    at = run_app()
    before = metric(at, "DCF price")
    at.sidebar.slider(key="rf").set_value(6.0).run()
    assert not at.exception
    assert float(metric(at, "DCF price").strip("$").replace(",", "")) < float(before.strip("$").replace(",", ""))


def test_company_view_changes_its_price_and_survives_switching():
    at = run_app()
    ticker = at.selectbox(key="company").value
    before = metric(at, "DCF price")
    at.checkbox(key=f"on_{ticker}").check().run()
    at.slider(key=f"g_{ticker}").set_value(45.0).run()
    assert not at.exception
    assert metric(at, "DCF price") != before

    other = next(o for o in at.selectbox(key="company").options if not o.startswith(ticker))
    at.selectbox(key="company").set_value(other.split(" · ")[0]).run()
    assert ticker in at.session_state["company_views"]
