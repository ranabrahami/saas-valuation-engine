"""Tests for DCF math, checked against hand calculations."""

import numpy as np
import pandas as pd
import pytest

from saas_valuation.models.dcf import (adjusted_beta, cost_of_equity, discount, enterprise_value,
                                       implied_price, linear_path, project, terminal_value, wacc)


def test_capm_and_blume_beta():
    assert adjusted_beta(1.6, 1.2, blume=True)[0] == pytest.approx(2 / 3 * 1.6 + 1 / 3)
    assert adjusted_beta(None, 1.2, blume=True) == (1.2, "default (no beta available)")
    assert cost_of_equity(1.2, 0.04, 0.05) == pytest.approx(0.10)


def test_wacc_weights_and_tax_shield():
    assert wacc(100, 0, 0.10, 0.06, 0.21) == pytest.approx(0.10)  # no debt -> cost of equity
    assert wacc(75, 25, 0.10, 0.06, 0.21) == pytest.approx(0.75 * 0.10 + 0.25 * 0.06 * 0.79)


def test_linear_path_reaches_target_in_final_year():
    assert linear_path(0.30, 0.03, 3) == pytest.approx([0.21, 0.12, 0.03])


def test_projection_compounds_revenue():
    p = project(100, 0.10, 0.20, 0.10, 0.20, years=2, first_year=2026)
    assert list(p.index) == [2026, 2027]
    assert p["revenue"].tolist() == pytest.approx([110, 121])
    assert p["fcf"].tolist() == pytest.approx([22, 24.2])


def test_mid_year_discounting():
    d = discount(pd.DataFrame({"fcf": [100.0, 100.0]}), 0.10)
    assert d["period"].tolist() == [0.5, 1.5]
    assert d["pv_fcf"].tolist() == pytest.approx([100 / 1.1 ** 0.5, 100 / 1.1 ** 1.5])


def test_gordon_growth_and_its_limit():
    assert terminal_value(100, 0.10, 0.03) == pytest.approx(103 / 0.07)
    assert np.isnan(terminal_value(100, 0.03, 0.03))  # WACC must exceed growth


def test_enterprise_value_matches_hand_calculation():
    p = pd.DataFrame({"fcf": [100.0]}, index=[2026])
    v = enterprise_value(p, 0.10, 0.0)
    hand = 100 / 1.1 ** 0.5 + (100 / 0.10) / 1.1
    assert v["enterprise_value"] == pytest.approx(hand)


def test_implied_price_and_negative_equity():
    assert implied_price(ev=900, net_cash=100, roll_forward=1.1, shares=10) == pytest.approx(110)
    assert np.isnan(implied_price(ev=50, net_cash=-100, roll_forward=1.0, shares=10))


def test_reverse_dcf_recovers_the_growth_that_produced_a_price():
    from saas_valuation.models.dcf import market_implied_growth

    def price_at(growth):  # simple increasing price function
        return 100 * (1 + growth) ** 5

    assert market_implied_growth(price_at(0.12), price_at) == pytest.approx(0.12, abs=1e-3)
    assert np.isnan(market_implied_growth(1e9, price_at))  # unreachable price
