"""Price targets, scenarios and ratings."""

import numpy as np
import pandas as pd
import pytest

from saas_valuation.config import load_config, project_path
from saas_valuation.models.target import blend, price_targets, rate, residual_sd, roll_forward, round_target

CFG = load_config()
PROCESSED = project_path(CFG["data"]["processed_dir"])


def test_roll_forward_one_year_at_the_cost_of_equity():
    v = roll_forward(pd.Series([100.0]), pd.Series([0.10]), 12, 0.0)
    assert v.iloc[0] == pytest.approx(110.0)
    assert roll_forward(pd.Series([100.0]), pd.Series([0.10]), 6, 0.02).iloc[0] == pytest.approx(100 * 1.08 ** 0.5)


def test_targets_are_rounded():
    assert round_target(pd.Series([123.4, 123.6]), 1.0).tolist() == [123.0, 124.0]
    assert round_target(pd.Series([123.4]), 5.0).tolist() == [125.0]


def test_blend_falls_back_to_comps_when_dcf_is_not_meaningful():
    value, basis = blend(pd.Series([100.0, np.nan, -5.0]), pd.Series([200.0, 200.0, 200.0]), 0.5, 0.5)
    assert value.tolist() == [150.0, 200.0, 200.0]
    assert basis.tolist()[1:] == ["comps only (DCF not meaningful)"] * 2


def test_relative_rating_is_measured_against_the_median():
    er = pd.Series({"A": 0.40, "B": 0.10, "C": 0.05, "D": -0.20})
    labels, ref = rate(er, {"basis": "relative", "band": 0.15, "labels": {"up": "OW", "mid": "N", "down": "UW"}})
    assert ref == pytest.approx(0.075)
    assert labels.to_dict() == {"A": "OW", "B": "N", "C": "N", "D": "UW"}
    labels, _ = rate(er, {"basis": "absolute", "band": 0.15, "labels": {"up": "OW", "mid": "N", "down": "UW"}})
    assert labels["B"] == "N" and labels["D"] == "UW"


def test_residual_sd_is_zero_on_a_perfect_line():
    x = pd.Series([0.1, 0.2, 0.3, 0.4])
    assert residual_sd(x, np.exp(1 + 2 * x)) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.skipif(not (PROCESSED / "comps.csv").exists(), reason="run the pipeline and comps first")
def test_on_real_data():
    comps, bridge, market = (pd.read_csv(PROCESSED / f"{n}.csv", index_col="ticker")
                             for n in ("comps", "ev_bridge", "market_data"))
    t = price_targets(comps, bridge, market, CFG)["targets"]
    assert len(t) == len(comps)
    assert (t["bear_target"] <= t["price_target"]).all() and (t["price_target"] <= t["bull_target"]).all()
    assert t["rating"].nunique() == 3                      # not everything is a Buy
    assert (t["price_target"] % CFG["report"]["price_target"]["round_to"] == 0).all()
    assert (t["outlier"] == (t["expected_return"].abs() > CFG["report"]["price_target"]["flag_return_beyond"])).all()
