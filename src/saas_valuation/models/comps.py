"""Stage 3: trading comparables ("comps").

Comps answer a relative question: compared with similar companies, does
the market price this one cheaply or expensively? Absolute value is the
DCF's job (Stage 4). The two views are combined in Stage 5.

Run from the project root (after the data pipeline):
    python -m saas_valuation.models.comps

Conventions used in the output:
    NM   "not meaningful": the math works, but the result says nothing useful,
         e.g. EV/EBITDA when EBITDA is negative
    n/a  an input is missing from the data (see the Notes sheet)
"""

import numpy as np
import pandas as pd

from saas_valuation.config import load_config, project_path

FLOW_CONCEPTS = ("revenue", "gross_profit_calc", "operating_income", "depreciation_amortization",
                 "operating_cash_flow", "capex", "capitalized_software", "stock_based_comp")
NEEDED_COLUMNS = ("revenue", "gross_profit", "cost_of_revenue", "operating_income",
                  "depreciation_amortization", "operating_cash_flow", "capex", "capitalized_software",
                  "stock_based_comp", "cash", "short_term_investments", "debt_total", "debt_current",
                  "debt_noncurrent", "diluted_shares", "diluted_shares_split_adj")

VALUATION_COLUMNS = ["price", "market_cap", "enterprise_value", "ev_revenue", "ev_ebitda", "p_fcf"]
OPERATING_COLUMNS = ["revenue", "revenue_growth", "gross_margin", "ebitda_margin", "fcf_margin",
                     "fcf_margin_after_sbc", "sbc_pct_revenue", "rule_of_40", "net_dilution"]


# ---------------------------------------------------------------------------
# Small helpers that return (value, label); label is "", "n/a" or "NM"
# ---------------------------------------------------------------------------

def multiple(numerator: float, denominator: float, cap: float | None = None) -> tuple[float, str]:
    """Valuation multiple. Negative earnings or value make a multiple meaningless."""
    if pd.isna(numerator) or pd.isna(denominator):
        return np.nan, "n/a"
    if numerator <= 0 or denominator <= 0:
        return np.nan, "NM"
    value = numerator / denominator
    if cap is not None and value > cap:
        return np.nan, "NM"
    return value, ""


def margin(numerator: float, denominator: float) -> tuple[float, str]:
    """Margin or growth-style ratio. A negative numerator is still informative."""
    if pd.isna(numerator) or pd.isna(denominator):
        return np.nan, "n/a"
    if denominator <= 0:
        return np.nan, "NM"
    return numerator / denominator, ""


# ---------------------------------------------------------------------------
# Calendarization
# ---------------------------------------------------------------------------

def calendar_weights(fye_month: int, year: int) -> dict[int, float]:
    """Which fiscal years (labelled by the year they end) cover calendar `year`, and how much.

    A fiscal year ending January 2026 (FY2026) runs Feb 2025 - Jan 2026, so it
    holds 11 months of CY2025; FY2025 holds the other 1 (January 2025).
    """
    if fye_month == 12:
        return {year: 1.0}
    return {year: fye_month / 12, year + 1: (12 - fye_month) / 12}


def calendarize(company: pd.DataFrame, year: int,
                concepts: tuple[str, ...] = FLOW_CONCEPTS) -> tuple[dict[str, float], str]:
    """Blend one company's fiscal-year values into calendar-year values.

    Returns (values, explanation). If the later fiscal year isn't reported
    yet, the earlier one stands in as a proxy and the explanation says so.
    """
    rows = company.set_index("fiscal_year")
    month = int(company["period_end"].max()[5:7])
    weights = calendar_weights(month, year)
    note = ("fiscal year = calendar year" if month == 12 else
            " + ".join(f"{round(w * 12)}/12 × FY{fy}" for fy, w in weights.items()))

    if any(fy not in rows.index for fy in weights):
        if year not in rows.index:
            return {c: np.nan for c in concepts}, f"no fiscal year covering CY{year}"
        note = (f"FY{year} used as proxy (covers {month}/12 of CY{year}; "
                f"FY{year + 1} not reported yet)")
        weights = {year: 1.0}

    values = {}
    for concept in concepts:
        parts = [rows.at[fy, concept] for fy in weights]
        values[concept] = (sum(w * v for w, v in zip(weights.values(), parts))
                           if all(pd.notna(p) for p in parts) else np.nan)
    return values, note


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def prepare_fiscal(financials: pd.DataFrame) -> pd.DataFrame:
    """Per-fiscal-year inputs that need a rule before calendarizing."""
    fin = financials.reindex(columns=list(dict.fromkeys(
        list(financials.columns) + list(NEEDED_COLUMNS)))).copy()
    # Many SaaS filers don't tag gross profit; derive it where we can.
    fin["gross_profit_calc"] = fin["gross_profit"].fillna(fin["revenue"] - fin["cost_of_revenue"])
    # No capitalized-software line means the company expenses development
    # costs instead, which are already inside operating cash flow.
    fin["capitalized_software"] = fin["capitalized_software"].fillna(0.0)
    return fin


def total_debt(row: pd.Series) -> tuple[float, str]:
    """Total debt at the balance sheet date, and which rule produced it."""
    if pd.notna(row["debt_total"]):
        return row["debt_total"], "total long-term debt (incl. current portion)"
    current, noncurrent = row["debt_current"], row["debt_noncurrent"]
    if pd.isna(current) and pd.isna(noncurrent):
        return 0.0, "no debt reported; treated as 0 (verify in 10-K)"
    return np.nansum([current, noncurrent]), "current + noncurrent debt"


def summary_stats(df: pd.DataFrame) -> pd.DataFrame:
    """Max / quartiles / mean / median / min, ignoring NM and n/a."""
    return pd.DataFrame({
        "Max": df.max(), "75th Percentile": df.quantile(0.75), "Mean": df.mean(),
        "Median": df.median(), "25th Percentile": df.quantile(0.25), "Min": df.min(),
    }).T


# ---------------------------------------------------------------------------
# Main calculation
# ---------------------------------------------------------------------------

def compute_comps(financials: pd.DataFrame, market: pd.DataFrame, comps_cfg: dict,
                  splits: pd.DataFrame | None = None,
                  overrides: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Compute every comps metric. Returns a dict of tables, keyed by name."""
    year = comps_cfg["calendar_year"]
    cap = comps_cfg["max_meaningful_multiple"]
    fin = prepare_fiscal(financials)
    market = market.set_index("ticker")

    rows, labels, bridge, calendar, notes = [], [], [], [], []

    for ticker, company in fin.groupby("ticker", sort=False):
        company = company.sort_values("fiscal_year")
        latest = company.iloc[-1]
        mkt = market.loc[ticker] if ticker in market.index else pd.Series(dtype=object)
        ticker_notes = []

        cy, cy_note = calendarize(company, year)
        prior, prior_note = calendarize(company, year - 1)
        if "proxy" in cy_note:
            ticker_notes.append(f"CY{year}: {cy_note}")

        # --- Enterprise value bridge (balance sheet at latest fiscal year-end) ---
        market_cap = mkt.get("market_cap", np.nan)
        debt, debt_note = total_debt(latest)
        investments = (latest["short_term_investments"] if comps_cfg["ev_net_short_term_investments"]
                       and pd.notna(latest["short_term_investments"]) else 0.0)
        ev = market_cap + debt - latest["cash"] - investments
        if "treated as 0" in debt_note:
            ticker_notes.append(f"Debt: {debt_note}")
        bridge.append({"ticker": ticker, "market_cap": market_cap, "debt": debt,
                       "cash": latest["cash"], "short_term_investments": investments,
                       "enterprise_value": ev, "balance_sheet_date": latest["period_end"],
                       "debt_basis": debt_note})

        # --- Calendar-year earnings and cash flow ---
        ebitda = cy["operating_income"] + cy["depreciation_amortization"]
        if comps_cfg["ebitda_add_back_sbc"]:
            ebitda += cy["stock_based_comp"]
        fcf = cy["operating_cash_flow"] - cy["capex"]
        if comps_cfg["fcf_subtract_capitalized_software"]:
            fcf -= cy["capitalized_software"]
        fcf_after_sbc = fcf - cy["stock_based_comp"]

        for concept, label in (("capex", "FCF"), ("depreciation_amortization", "EBITDA"),
                               ("gross_profit_calc", "gross margin")):
            if pd.isna(cy[concept]):
                ticker_notes.append(f"{label} n/a: {concept.replace('_calc', '')} missing for a "
                                    f"fiscal year in CY{year} (see overrides in config.yaml)")

        # --- Metrics ---
        values, value_labels = {}, {}

        def put(name, result):
            values[name], value_labels[name] = result

        put("price", (mkt.get("price", np.nan), ""))
        put("market_cap", (market_cap, ""))
        put("enterprise_value", (ev, "" if pd.notna(ev) else "n/a"))
        put("ev_revenue", multiple(ev, cy["revenue"], cap))
        put("ev_ebitda", multiple(ev, ebitda, cap))
        put("p_fcf", multiple(market_cap, fcf, cap))

        put("revenue", (cy["revenue"], "" if pd.notna(cy["revenue"]) else "n/a"))
        growth = margin(cy["revenue"] - prior["revenue"], prior["revenue"])
        put("revenue_growth", growth)
        put("gross_margin", margin(cy["gross_profit_calc"], cy["revenue"]))
        put("ebitda_margin", margin(ebitda, cy["revenue"]))
        put("fcf_margin", margin(fcf, cy["revenue"]))
        put("fcf_margin_after_sbc", margin(fcf_after_sbc, cy["revenue"]))
        put("sbc_pct_revenue", margin(cy["stock_based_comp"], cy["revenue"]))

        r40_margin = comps_cfg["rule_of_40_margin"]
        if value_labels["revenue_growth"] or value_labels[r40_margin]:
            put("rule_of_40", (np.nan, value_labels["revenue_growth"] or value_labels[r40_margin]))
        else:
            put("rule_of_40", (values["revenue_growth"] + values[r40_margin], ""))

        # Net dilution: year-over-year change in diluted shares, on today's
        # split-adjusted basis so a split isn't mistaken for share issuance.
        shares = company["diluted_shares_split_adj"].dropna()
        put("net_dilution", margin(shares.iloc[-1] - shares.iloc[-2], shares.iloc[-2])
            if len(shares) >= 2 else (np.nan, "n/a"))

        if splits is not None:
            for _, s in splits[(splits["ticker"] == ticker) & splits["in_data_window"]].iterrows():
                ticker_notes.append(f"Stock split: {s['ratio']:g}-for-1 on {s['date']}; "
                                    "share counts shown on post-split basis")

        if overrides is not None and not overrides.empty:
            for _, o in overrides[(overrides["ticker"] == ticker)].iterrows():
                ticker_notes.append(f"Override {o['status']}: {o['concept']} FY{o['fiscal_year']}")

        rows.append({"ticker": ticker, "name": mkt.get("name", ticker),
                     "bucket": latest["bucket"], "fye_month": int(latest["period_end"][5:7]), **values})
        labels.append({"ticker": ticker, **value_labels})
        calendar.append({"ticker": ticker, "fye_month": int(latest["period_end"][5:7]),
                         f"cy{year}_method": cy_note, f"cy{year - 1}_method": prior_note,
                         f"revenue_cy{year}": cy["revenue"], f"revenue_cy{year - 1}": prior["revenue"]})
        notes.append({"ticker": ticker, "notes": ticker_notes})

    comps = pd.DataFrame(rows).set_index("ticker")
    label_df = pd.DataFrame(labels).set_index("ticker")
    numeric = VALUATION_COLUMNS[3:] + OPERATING_COLUMNS[1:]
    bucket_medians = comps.groupby("bucket")[numeric].median()
    bucket_medians.insert(0, "companies", comps.groupby("bucket").size())

    return {
        "comps": comps,
        "labels": label_df,
        "valuation_stats": summary_stats(comps[VALUATION_COLUMNS[3:]]),
        "operating_stats": summary_stats(comps[OPERATING_COLUMNS[1:]]),
        "bucket_medians": bucket_medians,
        "ev_bridge": pd.DataFrame(bridge).set_index("ticker"),
        "calendarization": pd.DataFrame(calendar).set_index("ticker"),
        "notes": pd.DataFrame(notes).set_index("ticker"),
    }


def run() -> None:
    from saas_valuation.reports.comps_excel import write_comps_workbook

    cfg = load_config()
    comps_cfg = cfg["comps"]
    processed = project_path(cfg["data"]["processed_dir"])

    financials = pd.read_csv(processed / "financials.csv")
    market = pd.read_csv(processed / "market_data.csv")
    splits = pd.read_csv(processed / "stock_splits.csv")
    overrides = pd.read_csv(processed / "overrides_log.csv")

    tables = compute_comps(financials, market, comps_cfg, splits, overrides)
    tables["comps"].to_csv(processed / "comps.csv")
    tables["ev_bridge"].to_csv(processed / "ev_bridge.csv")

    out_dir = project_path(comps_cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"comps_CY{comps_cfg['calendar_year']}.xlsx"
    market_date = market["fetched_at"].max()[:10]
    write_comps_workbook(path, tables, cfg, splits, overrides, market_date)

    comps, labels = tables["comps"], tables["labels"]
    shown = comps[["ev_revenue", "ev_ebitda", "p_fcf", "revenue_growth", "fcf_margin", "rule_of_40"]]
    shown = shown.sort_values("rule_of_40", ascending=False)
    display = shown.copy().astype(object)
    for col in shown.columns:
        fmt = "{:.1f}x" if col in ("ev_revenue", "ev_ebitda", "p_fcf") else "{:.0%}"
        display[col] = [labels.at[t, col] or fmt.format(v) for t, v in shown[col].items()]
    print(display.to_string())
    print(f"\nWrote {path}")


if __name__ == "__main__":
    run()
