"""Tests for EDGAR extraction, using small hand-built companyfacts payloads
so they run offline and pin down each pitfall the module handles."""

from saas_valuation.data.edgar import extract_company
from saas_valuation.data.tags import Concept

REVENUE = Concept("revenue", ("RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
                  "duration", "USD", "error", "")
CASH = Concept("cash", ("CashAndCashEquivalentsAtCarryingValue",), "instant", "USD", "error", "")
DEBT = Concept("debt_noncurrent", ("LongTermDebtNoncurrent",), "instant", "USD", "warning", "")


def fact(val, end, start=None, form="10-K", filed="2024-03-01"):
    f = {"val": val, "end": end, "form": form, "filed": filed}
    if start:
        f["start"] = start
    return f


def facts(**tags):
    return {"facts": {"us-gaap": {tag: {"units": {"USD": rows}} for tag, rows in tags.items()}}}


def run(payload, concepts=(REVENUE,), years=5):
    return extract_company("TEST", payload, 350, 380, years, concepts)


def values(records, concept="revenue"):
    return {r["fiscal_year"]: r["value"] for r in records if r["concept"] == concept}


def test_latest_filing_wins_for_duplicate_periods():
    payload = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
        fact(100, "2023-12-31", "2023-01-01", filed="2024-02-01"),
        fact(105, "2023-12-31", "2023-01-01", filed="2025-02-01"),  # restated in next 10-K
    ])
    records, _ = run(payload)
    assert values(records) == {2023: 105}


def test_quarters_and_10q_facts_are_ignored():
    payload = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
        fact(400, "2023-12-31", "2023-01-01"),
        fact(110, "2023-12-31", "2023-10-01"),                     # Q4 inside a 10-K
        fact(999, "2023-12-31", "2023-01-01", form="10-Q"),        # wrong form
    ])
    records, _ = run(payload)
    assert values(records) == {2023: 400}


def test_tag_fallback_across_years_is_used_and_flagged():
    payload = facts(
        SalesRevenueNet=[fact(80, "2017-12-31", "2017-01-01")],
        RevenueFromContractWithCustomerExcludingAssessedTax=[fact(90, "2018-12-31", "2018-01-01")],
    )
    records, issues = run(payload)
    assert values(records) == {2017: 80, 2018: 90}
    assert {r["fiscal_year"]: r["tag"] for r in records}[2017] == "SalesRevenueNet"
    assert any(i["issue"] == "tag_changed" and i["fiscal_year"] == 2018 for i in issues)


def test_balance_sheet_matched_at_fiscal_year_end_and_gaps_flagged():
    payload = facts(
        RevenueFromContractWithCustomerExcludingAssessedTax=[
            fact(100, "2023-01-31", "2022-02-01"), fact(120, "2024-01-31", "2023-02-01")],
        CashAndCashEquivalentsAtCarryingValue=[
            fact(50, "2023-01-31"), fact(60, "2024-01-31"), fact(55, "2023-07-31")],  # mid-year ignored
    )
    records, issues = run(payload, concepts=(REVENUE, CASH, DEBT))
    assert values(records, "cash") == {2023: 50, 2024: 60}
    missing_debt = [i for i in issues if i["concept"] == "debt_noncurrent"]
    assert len(missing_debt) == 2 and all(i["severity"] == "warning" for i in missing_debt)
    assert any(i["issue"] == "non_december_fye" for i in issues)


def test_only_last_n_years_kept_and_short_history_flagged():
    rows = [fact(100 + y, f"{y}-12-31", f"{y}-01-01") for y in range(2016, 2025)]
    records, _ = run(facts(RevenueFromContractWithCustomerExcludingAssessedTax=rows))
    assert sorted(values(records)) == [2020, 2021, 2022, 2023, 2024]

    _, issues = run(facts(RevenueFromContractWithCustomerExcludingAssessedTax=rows[-2:]))
    assert any(i["issue"] == "short_history" for i in issues)


def test_no_revenue_is_an_error():
    records, issues = run(facts())
    assert records == []
    assert issues[0]["severity"] == "error"


def test_company_with_no_debt_gets_one_warning_not_one_per_year():
    debt = Concept("debt_noncurrent", ("LongTermDebtNoncurrent",), "instant", "USD", "info", "")
    payload = facts(RevenueFromContractWithCustomerExcludingAssessedTax=[
        fact(100, "2023-12-31", "2023-01-01"), fact(110, "2024-12-31", "2024-01-01")])
    _, issues = run(payload, concepts=(REVENUE, debt), years=2)
    warnings = [i for i in issues if i["severity"] == "warning"]
    assert [i["issue"] for i in warnings] == ["no_debt_found"]


def test_reported_top_line_preferred_over_contract_revenue():
    from saas_valuation.data.tags import CONCEPTS
    revenue = next(c for c in CONCEPTS if c.name == "revenue")
    payload = facts(
        Revenues=[fact(1175, "2023-06-30", "2022-07-01")],  # includes interest on client funds
        RevenueFromContractWithCustomerExcludingAssessedTax=[fact(1098, "2023-06-30", "2022-07-01")],
    )
    records, _ = run(payload, concepts=(revenue,))
    assert values(records) == {2023: 1175}
