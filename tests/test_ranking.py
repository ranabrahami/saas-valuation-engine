"""Tests for the ranking: regression, leave-one-out, score combination, verdicts."""

import numpy as np
import pandas as pd
import pytest

from saas_valuation.models.ranking import (combine_scores, fair_multiples, fit_log_multiple,
                                           percentile_score, verdict)


def test_log_fit_recovers_exact_relationship():
    x = pd.Series([0.05, 0.10, 0.20, 0.30])
    multiple = np.exp(1.0 + 5.0 * x)
    a, b, r2 = fit_log_multiple(x, multiple)
    assert (a, b, r2) == pytest.approx((1.0, 5.0, 1.0))


def test_leave_one_out_stops_an_outlier_setting_its_own_benchmark():
    x = pd.Series([0.05, 0.10, 0.15, 0.20, 0.20], index=list("ABCDE"))
    multiple = pd.Series(np.exp(1 + 5 * x.values), index=x.index)
    multiple["E"] = 100.0  # wildly expensive for its growth
    in_sample = fair_multiples(x, multiple, leave_one_out=False)
    held_out = fair_multiples(x, multiple, leave_one_out=True)
    on_line = np.exp(1 + 5 * 0.20)
    assert held_out["E"] == pytest.approx(on_line)   # judged only against A-D
    assert in_sample["E"] > held_out["E"]            # including itself flatters it


def test_percentiles_and_weighted_score():
    dcf = percentile_score(pd.Series([0.5, -0.2, 0.1], index=list("ABC")))
    assert dcf.tolist() == pytest.approx([100, 100 / 3, 200 / 3])
    comps = pd.Series([50.0, np.nan, 100.0], index=list("ABC"))
    score = combine_scores(dcf, comps, 0.5, 0.5)
    assert score["A"] == pytest.approx(75)
    assert score["B"] == pytest.approx(100 / 3)  # missing comps -> DCF alone


def test_verdict_thresholds():
    assert [verdict(s, 67, 33) for s in (90, 67, 50, 33, 10)] == [
        "Undervalued", "Undervalued", "In line", "Overvalued", "Overvalued"]
    assert verdict(np.nan, 67, 33) == "n/a"
