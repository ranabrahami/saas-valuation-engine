"""Stage 4: discounted cash flow (DCF) valuation for each company.

A DCF says a business is worth the cash it will generate, discounted back
to today at the return investors require for bearing its risk. Unlike the
comps, it doesn't depend on how the market prices similar companies.

Steps for each company:
  1. Discount rate: WACC, with the cost of equity from CAPM.
  2. Project revenue and free cash flow: 5 explicit years, then 5 fade
     years in which growth glides down to the terminal rate.
  3. Terminal value: everything after year 10, using the Gordon growth formula.
  4. Discount it all to Jan 1 of the first projection year -> enterprise value.
  5. EV + cash - debt = equity value; roll forward to today; divide by shares.
  6. Sensitivity grid: implied price across WACC x terminal growth.

Run from the project root (after the pipeline and comps):
    python -m saas_valuation.models.dcf
"""

from datetime import date

import numpy as np
import pandas as pd

from saas_valuation.config import load_config, project_path


# ---------------------------------------------------------------------------
# Discount rate
# ---------------------------------------------------------------------------

def adjusted_beta(raw_beta: float | None, default: float, blume: bool) -> tuple[float, str]:
    if raw_beta is None or pd.isna(raw_beta):
        return default, "default (no beta available)"
    if blume:
        return 2 / 3 * raw_beta + 1 / 3, f"Blume-adjusted from raw {raw_beta:.2f}"
    return raw_beta, "raw"


def cost_of_equity(beta: float, risk_free: float, erp: float) -> float:
    """CAPM: investors demand the risk-free rate plus beta x the market's risk premium."""
    return risk_free + beta * erp


def wacc(equity_value: float, debt: float, k_equity: float, k_debt_pre_tax: float,
         tax_rate: float) -> float:
    """Weighted average cost of capital. Interest is tax-deductible, hence (1 - tax)."""
    total = equity_value + debt
    return equity_value / total * k_equity + debt / total * k_debt_pre_tax * (1 - tax_rate)


# ---------------------------------------------------------------------------
# Projection and terminal value
# ---------------------------------------------------------------------------

def linear_path(start: float, end: float, years: int) -> list[float]:
    """Values for years 1..N moving in equal steps from `start` (year 0) to `end` (year N)."""
    return [start + (end - start) * t / years for t in range(1, years + 1)]


def project(base_revenue: float, start_growth: float, start_margin: float, end_growth: float,
            end_margin: float, years: int, first_year: int, explicit_years: int | None = None) -> pd.DataFrame:
    """Revenue and FCF for `years` years. Years after `explicit_years` are labelled the fade stage."""
    explicit_years = explicit_years or years
    growth = linear_path(start_growth, end_growth, years)
    margins = linear_path(start_margin, end_margin, years)
    revenue, rows = base_revenue, []
    for i in range(years):
        revenue *= 1 + growth[i]
        rows.append({"year": first_year + i, "stage": "explicit" if i < explicit_years else "fade",
                     "revenue": revenue, "growth": growth[i],
                     "fcf_margin": margins[i], "fcf": revenue * margins[i]})
    return pd.DataFrame(rows).set_index("year")


def discount(projection: pd.DataFrame, rate: float) -> pd.DataFrame:
    """Mid-year convention: a year's cash arrives through the year, so on
    average halfway through it. Year 1 is discounted 0.5 years, year 2 1.5..."""
    out = projection.copy()
    out["period"] = np.arange(1, len(out) + 1) - 0.5
    out["discount_factor"] = 1 / (1 + rate) ** out["period"]
    out["pv_fcf"] = out["fcf"] * out["discount_factor"]
    return out


def terminal_value(final_fcf: float, rate: float, growth: float) -> float:
    """Gordon growth: value at the end of the final year of all cash flows after it."""
    if rate <= growth:
        return np.nan
    return final_fcf * (1 + growth) / (rate - growth)


def enterprise_value(projection: pd.DataFrame, rate: float, growth: float) -> dict:
    discounted = discount(projection, rate)
    tv = terminal_value(projection["fcf"].iloc[-1], rate, growth)
    pv_tv = tv / (1 + rate) ** len(projection)
    return {"discounted": discounted, "terminal_value": tv, "pv_terminal_value": pv_tv,
            "sum_pv_fcf": discounted["pv_fcf"].sum(),
            "enterprise_value": discounted["pv_fcf"].sum() + pv_tv}


def implied_price(ev: float, net_cash: float, roll_forward: float, shares: float) -> float:
    equity = (ev + net_cash) * roll_forward
    return equity / shares if equity > 0 else np.nan


def market_implied_growth(target_price: float, price_at_growth, low: float = -0.3,
                          high: float = 1.5, tolerance: float = 1e-4) -> float:
    """Reverse DCF: the starting growth that makes the DCF price equal today's price.

    Holds every other assumption fixed and solves by bisection. NaN if no
    growth rate in [low, high] gets there.
    """
    def gap(growth):
        price = price_at_growth(growth)
        return (price if pd.notna(price) else 0.0) - target_price

    if gap(low) > 0 or gap(high) < 0:
        return np.nan
    while high - low > tolerance:
        mid = (low + high) / 2
        low, high = (mid, high) if gap(mid) < 0 else (low, mid)
    return (low + high) / 2


# ---------------------------------------------------------------------------
# One company
# ---------------------------------------------------------------------------

def value_company(ticker: str, comps: pd.Series, bridge: pd.Series, market: pd.Series,
                  cfg: dict, first_year: int, years_elapsed: float) -> dict:
    d = cfg["dcf"]
    override = (d.get("company_overrides") or {}).get(ticker, {})
    notes = []

    # 1. Discount rate
    if "beta" in override:
        beta, beta_note = override["beta"], "company override"
    else:
        beta, beta_note = adjusted_beta(market.get("beta"), d["default_beta"], d["blume_adjust_beta"])
    k_e = cost_of_equity(beta, d["risk_free_rate"], d["equity_risk_premium"])
    discount_rate = wacc(market["market_cap"], bridge["debt"], k_e, d["pre_tax_cost_of_debt"], d["tax_rate"])

    # 2. Projection
    margin_col = "fcf_margin_after_sbc" if d["sbc_treatment"] == "expense" else "fcf_margin"
    start_margin = comps[margin_col]
    start_growth = override.get("starting_growth", min(comps["revenue_growth"], d["max_starting_growth"]))
    if comps["revenue_growth"] > d["max_starting_growth"] and "starting_growth" not in override:
        notes.append(f"Starting growth capped at {d['max_starting_growth']:.0%} "
                     f"(actual {comps['revenue_growth']:.0%})")
    target_margin = override.get("target_fcf_margin", max(start_margin, d["target_fcf_margin"]))
    g = d["terminal_growth_rate"]
    total_years = d["projection_years"] + d["fade_years"]
    projection = project(comps["revenue"], start_growth, start_margin, g, target_margin,
                         total_years, first_year, d["projection_years"])

    # 3-4. Terminal value and enterprise value
    valuation = enterprise_value(projection, discount_rate, g)
    ev = valuation["enterprise_value"]

    # 5. Equity value and price
    net_cash = bridge["cash"] + bridge["short_term_investments"] - bridge["debt"]
    roll = (1 + k_e) ** years_elapsed
    shares = market["implied_shares"]
    price = implied_price(ev, net_cash, roll, shares)
    if pd.isna(price):
        notes.append("Implied equity value is negative: projected cash flows don't cover the "
                     "current balance sheet. Shown as NM.")

    # Reverse DCF: what starting growth is today's price assuming?
    def price_at_growth(growth: float) -> float:
        p = project(comps["revenue"], growth, start_margin, g, target_margin, total_years,
                    first_year, d["projection_years"])
        return implied_price(enterprise_value(p, discount_rate, g)["enterprise_value"], net_cash, roll, shares)

    implied_growth = market_implied_growth(market["price"], price_at_growth)

    # 6. Sensitivity
    sens = d["sensitivity"]
    grid = pd.DataFrame(index=[discount_rate + dw for dw in sens["wacc_range"]],
                        columns=sens["terminal_growth_range"], dtype=float)
    for r in grid.index:
        for tg in grid.columns:
            grid.at[r, tg] = implied_price(enterprise_value(projection, r, tg)["enterprise_value"],
                                           net_cash, (1 + k_e) ** years_elapsed, shares)
    grid.index.name, grid.columns.name = "WACC", "Terminal growth"

    final = projection.iloc[-1]
    summary = {
        "ticker": ticker, "price": market["price"], "implied_price": price,
        "upside": price / market["price"] - 1 if pd.notna(price) else np.nan,
        "beta": beta, "beta_basis": beta_note, "cost_of_equity": k_e, "wacc": discount_rate,
        "debt_weight": bridge["debt"] / (market["market_cap"] + bridge["debt"]),
        "start_growth": start_growth,
        "market_implied_growth": implied_growth,
        "year5_growth": projection["growth"].iloc[d["projection_years"] - 1],
        "start_fcf_margin": start_margin, "target_fcf_margin": target_margin,
        "sum_pv_fcf": valuation["sum_pv_fcf"], "pv_terminal_value": valuation["pv_terminal_value"],
        "enterprise_value": ev,
        "tv_pct_ev": valuation["pv_terminal_value"] / ev if ev > 0 else np.nan,
        "implied_terminal_ev_revenue": valuation["terminal_value"] / final["revenue"],
        "net_cash": net_cash, "roll_forward_factor": roll,
        "equity_value": (ev + net_cash) * roll, "shares": shares,
        "market_ev_revenue": comps["ev_revenue"],
        "notes": " | ".join(notes),
    }
    return {"summary": summary, "projection": valuation["discounted"], "sensitivity": grid,
            "base_revenue": comps["revenue"], "base_year": first_year - 1}


def value_universe(comps: pd.DataFrame, bridge: pd.DataFrame, market: pd.DataFrame,
                   cfg: dict) -> tuple[pd.DataFrame, dict, date, float]:
    """Value every company under one set of assumptions.

    Returns (summary table, per-company results, valuation date, years rolled forward).
    The dashboard calls this on every assumption change.
    """
    first_year = cfg["comps"]["calendar_year"] + 1
    valuation_date = cfg["dcf"].get("valuation_date") or date.today()
    if isinstance(valuation_date, str):
        valuation_date = date.fromisoformat(valuation_date)
    years_elapsed = (valuation_date - date(first_year, 1, 1)).days / 365.25

    results = {t: value_company(t, comps.loc[t], bridge.loc[t], market.loc[t], cfg, first_year, years_elapsed)
               for t in comps.index}
    summary = pd.DataFrame([r["summary"] for r in results.values()]).set_index("ticker")
    summary.insert(0, "name", comps["name"])
    summary.insert(1, "bucket", comps["bucket"])
    return summary, results, valuation_date, years_elapsed


def run() -> None:
    from saas_valuation.reports.dcf_excel import write_dcf_workbook

    cfg = load_config()
    processed = project_path(cfg["data"]["processed_dir"])
    comps = pd.read_csv(processed / "comps.csv", index_col="ticker")
    bridge = pd.read_csv(processed / "ev_bridge.csv", index_col="ticker")
    market = pd.read_csv(processed / "market_data.csv", index_col="ticker")

    summary, results, valuation_date, years_elapsed = value_universe(comps, bridge, market, cfg)
    first_year = cfg["comps"]["calendar_year"] + 1
    summary.to_csv(processed / "dcf_summary.csv")

    out = project_path(cfg["comps"]["output_dir"]) / "dcf_models.xlsx"
    write_dcf_workbook(out, summary, results, cfg, valuation_date)

    table = summary[["wacc", "start_growth", "market_implied_growth", "start_fcf_margin", "price",
                     "implied_price", "upside", "tv_pct_ev"]].sort_values("upside", ascending=False)
    print(f"Valuation date {valuation_date} (rolled forward {years_elapsed:.2f} years from {first_year}-01-01)\n")
    print(table.to_string(formatters={
        "wacc": "{:.1%}".format, "start_growth": "{:.0%}".format, "start_fcf_margin": "{:.0%}".format,
        "market_implied_growth": lambda v: ">150%" if pd.isna(v) else f"{v:.0%}",
        "price": "${:,.2f}".format, "implied_price": lambda v: "NM" if pd.isna(v) else f"${v:,.2f}",
        "upside": lambda v: "NM" if pd.isna(v) else f"{v:+.0%}", "tv_pct_ev": "{:.0%}".format}))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    run()
