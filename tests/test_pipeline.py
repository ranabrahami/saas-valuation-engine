"""Tests for the share-count cross-checks in the pipeline."""

import pandas as pd

from saas_valuation.data.pipeline import share_count_checks


def market(ticker, implied, yf_shares=None, cover=None):
    return {"ticker": ticker, "implied_shares": implied, "shares_outstanding": yf_shares or implied,
            "sec_cover_shares": cover, "sec_cover_date": "2026-08-01" if cover else None}


def check(rows, diluted):
    fin = pd.DataFrame([{"ticker": t, "fiscal_year": 2026, "diluted_shares": d} for t, d in diluted.items()])
    return {(i["ticker"], i["issue"]) for i in share_count_checks(fin, pd.DataFrame(rows), 0.15)}


def test_four_for_one_split_detected():
    assert ("SPLT", "likely_stock_split") in check([market("SPLT", 1_024e6)], {"SPLT": 250e6})


def test_dual_class_undercount_detected():
    found = check([market("DUAL", 253e6, yf_shares=159e6)], {"DUAL": 260e6})
    assert found == {("DUAL", "share_class_mismatch")}


def test_buyback_is_info_and_small_drift_is_silent():
    assert check([market("BUYB", 45e6)], {"BUYB": 56e6}) == {("BUYB", "share_count_changed")}
    assert check([market("OK", 389e6, cover=389e6)], {"OK": 400e6}) == set()
