"""Tests for comps math: calendarization, NM/n/a handling, debt rules, splits, overrides."""

import numpy as np
import pandas as pd
import pytest

from saas_valuation.data.adjustments import apply_overrides, split_factor
from saas_valuation.models.comps import calendar_weights, calendarize, margin, multiple, total_debt


def company(fye: str, revenue_by_fy: dict[int, float]) -> pd.DataFrame:
    return pd.DataFrame([{"fiscal_year": fy, "period_end": f"{fy}-{fye}", "revenue": rev}
                         for fy, rev in revenue_by_fy.items()])


def test_january_fye_is_mostly_the_following_fiscal_year():
    assert calendar_weights(1, 2025) == {2025: 1 / 12, 2026: 11 / 12}
    values, note = calendarize(company("01-31", {2025: 120, 2026: 240}), 2025, ("revenue",))
    assert values["revenue"] == pytest.approx(120 / 12 + 240 * 11 / 12)
    assert note == "1/12 × FY2025 + 11/12 × FY2026"


def test_december_fye_needs_no_blending():
    values, note = calendarize(company("12-31", {2024: 90, 2025: 100}), 2025, ("revenue",))
    assert values["revenue"] == 100 and note == "fiscal year = calendar year"


def test_missing_later_fiscal_year_falls_back_to_flagged_proxy():
    values, note = calendarize(company("11-28", {2024: 90, 2025: 100}), 2025, ("revenue",))
    assert values["revenue"] == 100
    assert "proxy" in note and "11/12" in note


def test_multiples_distinguish_not_meaningful_from_missing():
    assert multiple(100, 10) == (10, "")
    assert multiple(100, -5)[1] == "NM"        # negative EBITDA
    assert multiple(-50, 10)[1] == "NM"        # cash exceeds market cap
    assert multiple(1000, 5, cap=100)[1] == "NM"
    assert multiple(100, np.nan)[1] == "n/a"
    assert margin(-30, 100) == (-0.3, "")      # a negative margin is still informative


def test_total_debt_never_double_counts_current_portion():
    row = pd.Series({"debt_total": 500.0, "debt_current": 100.0, "debt_noncurrent": 400.0})
    assert total_debt(row)[0] == 500
    row = pd.Series({"debt_total": np.nan, "debt_current": 100.0, "debt_noncurrent": np.nan})
    assert total_debt(row)[0] == 100
    row = pd.Series({"debt_total": np.nan, "debt_current": np.nan, "debt_noncurrent": np.nan})
    value, note = total_debt(row)
    assert value == 0 and "verify" in note


def test_split_factor_applies_only_to_filings_before_the_split():
    splits = [{"date": "2025-12-18", "ratio": 5.0}]
    assert split_factor(splits, "2023-02-01") == 5.0   # pre-split 10-K
    assert split_factor(splits, "2026-01-29") == 1.0   # post-split 10-K, already restated
    assert split_factor(splits + [{"date": "2026-07-02", "ratio": 4.0}], "2023-02-01") == 20.0


def test_override_fills_gap_and_resolves_issue():
    records = [{"ticker": "VEEV", "fiscal_year": 2026, "period_end": "2026-01-31",
                "concept": "revenue", "value": 3.2e9}]
    issues = [{"ticker": "VEEV", "fiscal_year": 2026, "concept": "capex", "issue": "missing",
               "severity": "error", "detail": "No value."}]
    overrides = [{"ticker": "VEEV", "fiscal_year": 2026, "concept": "capex",
                  "value_usd_millions": 25.5, "source": "10-K p.80"},
                 {"ticker": "VEEV", "fiscal_year": 2025, "concept": "capex",
                  "value_usd_millions": None, "source": "10-K"}]
    records, log = apply_overrides(records, issues, overrides)
    capex = next(r for r in records if r["concept"] == "capex")
    assert capex["value"] == 25.5e6 and capex["tag"] == "override"
    assert issues[0]["severity"] == "resolved"
    assert [entry["status"] for entry in log] == ["applied", "pending"]
