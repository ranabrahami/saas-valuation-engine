"""The point-in-time backtest: no look-ahead, and the statistics behave."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from saas_valuation.config import load_config, project_path
from saas_valuation.research import backtest as bt

CFG = load_config()


def fact(end, filed, val, start=None):
    f = {"end": end, "filed": filed, "val": val, "form": "10-K", "fy": int(end[:4]), "fp": "FY"}
    if start:
        f["start"] = start
    return f


FACTS = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
    fact("2022-12-31", "2023-02-10", 100.0, "2022-01-01"),
    fact("2023-12-31", "2024-02-10", 120.0, "2023-01-01"),
    fact("2022-12-31", "2024-02-10", 101.0, "2022-01-01"),       # FY2022 restated in the next 10-K
]}}}}}


def test_facts_filed_after_the_date_are_removed():
    rows = bt.facts_as_of(FACTS, date(2023, 6, 30))["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    assert [r["val"] for r in rows] == [100.0]                       # the original FY2022 figure only
    rows = bt.facts_as_of(FACTS, date(2024, 6, 30))["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    assert sorted(r["val"] for r in rows) == [100.0, 101.0, 120.0]
    assert bt.facts_as_of(FACTS, date(2020, 1, 1))["facts"]["us-gaap"] == {}


def test_calendar_year_waits_for_the_10ks():
    assert bt.calendar_year_for(date(2024, 3, 31)) == 2023
    assert bt.calendar_year_for(date(2024, 1, 31)) == 2022            # CY2023 10-Ks not out yet
    assert bt.calendar_year_for(date(2024, 12, 31)) == 2023


def test_forward_return_needs_the_future_to_exist():
    idx = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    df = pd.DataFrame({"close": np.linspace(100, 200, len(idx)), "adj_close": np.linspace(100, 200, len(idx))}, index=idx)
    assert bt.forward_return(df, date(2024, 1, 1), 6) == pytest.approx(df.loc["2024-07-01", "adj_close"] / 100 - 1)
    assert np.isnan(bt.forward_return(df, date(2024, 6, 30), 12))


def test_newey_west_matches_plain_t_without_lags():
    x = pd.Series([0.1, 0.3, -0.1, 0.2, 0.25, 0.05])
    plain = x.mean() / (x.std(ddof=0) / np.sqrt(len(x)))
    assert bt.newey_west_t(x, 0) == pytest.approx(plain)
    assert abs(bt.newey_west_t(x, 3)) != pytest.approx(abs(plain))


def synthetic_panel(noise: float, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.date_range("2023-03-31", "2025-09-30", freq="3ME"):
        sig = rng.normal(size=18)
        for i in range(18):
            rows.append({"date": d.date().isoformat(), "ticker": f"T{i}", "score": sig[i],
                         **{k: sig[i] for k in bt.SIGNALS if k != "score"},
                         "fwd_12m": sig[i] + noise * rng.normal()})
    return pd.DataFrame(rows)


def test_a_perfect_signal_scores_ic_one_and_noise_scores_about_zero():
    _, perfect = bt.evaluate(synthetic_panel(0.0), 12, 3)
    assert perfect.loc[bt.SIGNALS["score"], "mean_ic"] == pytest.approx(1.0)
    random_panel = synthetic_panel(0.0).assign(fwd_12m=lambda d: np.random.default_rng(7).normal(size=len(d)))
    _, noise = bt.evaluate(random_panel, 12, 3)
    assert abs(noise.loc[bt.SIGNALS["score"], "mean_ic"]) < 0.15


def test_permutation_test_separates_signal_from_noise():
    assert bt.robustness(synthetic_panel(0.5), permutations=300)["permutation_p"] < 0.01
    noise = synthetic_panel(0.0).assign(fwd_12m=lambda d: np.random.default_rng(3).normal(size=len(d)))
    assert bt.robustness(noise, permutations=300)["permutation_p"] > 0.05


RAW = project_path(CFG["data"]["raw_dir"]) / "sec" / "company_tickers.json"


@pytest.mark.skipif(not RAW.exists(), reason="SEC data not downloaded")
def test_point_in_time_financials_contain_no_later_filings():
    inp = bt.load_inputs()
    as_of = date(2024, 6, 28)
    fin = bt.financials_as_of(inp, as_of)
    assert fin.groupby("ticker")["fiscal_year"].max().max() <= 2024
    adbe = fin[fin["ticker"] == "ADBE"]["fiscal_year"].max()
    assert adbe == 2023                                     # ADBE's FY2024 10-K was filed in January 2025
    sig = bt.signals_as_of(inp, as_of)
    assert len(sig) >= 15 and sig["calendar_year"].iloc[0] == 2023
