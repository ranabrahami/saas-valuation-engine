"""Stage 5: combine the DCF and comps views into one ranking.

The two signals answer different questions:
  * DCF upside:   is the price justified by the cash flows we can project?
  * Comps upside: is the price high or low relative to peers, given the
                  company's growth and profitability?
A company that looks cheap on both is a higher-conviction idea than one
that looks cheap on only one, so the output also flags agreement.

Run from the project root (after comps and DCF):
    python -m saas_valuation.models.ranking
"""

import numpy as np
import pandas as pd

from saas_valuation.config import load_config, project_path


def fit_log_multiple(x: pd.Series, multiple: pd.Series) -> tuple[float, float, float]:
    """OLS fit of ln(multiple) = a + b * x. Returns (a, b, r_squared).

    Logs because multiples are skewed: 48x vs 24x is the same "twice as
    expensive" as 8x vs 4x, and a log fit treats them that way.
    """
    y = np.log(multiple)
    b, a = np.polyfit(x, y, 1)
    residuals = y - (a + b * x)
    r_squared = 1 - (residuals ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return a, b, r_squared


def regressor_fit_comparison(comps: pd.DataFrame,
                             candidates=("revenue_growth", "rule_of_40", "fcf_margin")) -> dict[str, float]:
    """R² of ln(EV/Revenue) on each candidate metric: which does the market actually pay for?"""
    fits = {}
    for metric in candidates:
        valid = comps[metric].notna() & comps["ev_revenue"].notna()
        fits[metric] = fit_log_multiple(comps.loc[valid, metric], comps.loc[valid, "ev_revenue"])[2]
    return fits


def fair_multiples(x: pd.Series, multiple: pd.Series, leave_one_out: bool) -> pd.Series:
    """Predicted EV/Revenue for each company from the regression line."""
    valid = x.notna() & multiple.notna() & (multiple > 0)
    fair = pd.Series(np.nan, index=x.index)
    for ticker in x[valid].index:
        sample = valid & (x.index != ticker) if leave_one_out else valid
        a, b, _ = fit_log_multiple(x[sample], multiple[sample])
        fair[ticker] = np.exp(a + b * x[ticker])
    return fair


def percentile_score(values: pd.Series) -> pd.Series:
    """0-100: share of the universe this company beats on the signal."""
    return values.rank(pct=True) * 100


def combine_scores(dcf_pct: pd.Series, comps_pct: pd.Series, dcf_weight: float,
                   comps_weight: float) -> pd.Series:
    """Weighted average of percentile scores; if one signal is missing, use the other alone."""
    weights = pd.DataFrame({"dcf": dcf_pct.notna() * dcf_weight, "comps": comps_pct.notna() * comps_weight})
    total = (dcf_pct.fillna(0) * dcf_weight + comps_pct.fillna(0) * comps_weight)
    return total / weights.sum(axis=1).replace(0, np.nan)


def verdict(score: float, undervalued_above: float, overvalued_below: float) -> str:
    if pd.isna(score):
        return "n/a"
    if score >= undervalued_above:
        return "Undervalued"
    if score <= overvalued_below:
        return "Overvalued"
    return "In line"


def build_ranking(comps: pd.DataFrame, bridge: pd.DataFrame, market: pd.DataFrame,
                  dcf: pd.DataFrame, cfg: dict) -> dict[str, pd.DataFrame | dict]:
    r = cfg["ranking"]
    x = comps[r["comps_regressor"]]
    fair = fair_multiples(x, comps["ev_revenue"], r["leave_one_out"])

    net_cash = bridge["cash"] + bridge["short_term_investments"] - bridge["debt"]
    shares, price = market["implied_shares"], market["price"]
    comps_price = (fair * comps["revenue"] + net_cash) / shares
    comps_upside = comps_price / price - 1

    out = pd.DataFrame({
        "name": comps["name"], "bucket": comps["bucket"], "price": price,
        "dcf_price": dcf["implied_price"], "comps_price": comps_price,
        "dcf_upside": dcf["upside"], "comps_upside": comps_upside,
        "ev_revenue": comps["ev_revenue"], "fair_ev_revenue": fair, "regressor": x,
    })
    w_dcf, w_comps = r["dcf_upside_weight"], r["comps_weight"]
    out["blended_price"] = (out["dcf_price"] * w_dcf + out["comps_price"] * w_comps) / (w_dcf + w_comps)
    out["blended_upside"] = out["blended_price"] / price - 1
    out["dcf_score"] = percentile_score(out["dcf_upside"])
    out["comps_score"] = percentile_score(out["comps_upside"])
    out["score"] = combine_scores(out["dcf_score"], out["comps_score"], w_dcf, w_comps)
    out["verdict"] = out["score"].map(lambda s: verdict(s, r["undervalued_above"], r["overvalued_below"]))
    out["signals_agree"] = np.sign(out["dcf_upside"]) == np.sign(out["comps_upside"])
    out = out.sort_values("score", ascending=False)
    out.insert(0, "rank", range(1, len(out) + 1))

    valid = x.notna() & comps["ev_revenue"].notna()
    a, b, r2 = fit_log_multiple(x[valid], comps["ev_revenue"][valid])
    regression = {"intercept": a, "slope": b, "r_squared": r2, "regressor": r["comps_regressor"],
                  "n": int(valid.sum()),
                  "multiple_change_per_10pts": np.exp(b * 0.10) - 1,
                  "candidates": regressor_fit_comparison(comps)}
    return {"ranking": out, "regression": regression}


def run() -> None:
    from saas_valuation.reports.ranking_excel import write_ranking_workbook

    cfg = load_config()
    processed = project_path(cfg["data"]["processed_dir"])
    comps = pd.read_csv(processed / "comps.csv", index_col="ticker")
    bridge = pd.read_csv(processed / "ev_bridge.csv", index_col="ticker")
    market = pd.read_csv(processed / "market_data.csv", index_col="ticker")
    dcf = pd.read_csv(processed / "dcf_summary.csv", index_col="ticker")

    result = build_ranking(comps, bridge, market, dcf, cfg)
    ranking, reg = result["ranking"], result["regression"]
    ranking.to_csv(processed / "ranking.csv")

    path = project_path(cfg["comps"]["output_dir"]) / "ranking.xlsx"
    write_ranking_workbook(path, ranking, reg, cfg)

    print(f"Comps regression: ln(EV/Revenue) on {reg['regressor']}, R² = {reg['r_squared']:.2f} "
          f"(n={reg['n']}); +10 pts -> multiple {reg['multiple_change_per_10pts']:+.0%}\n")
    pct = "{:+.0%}".format
    print(ranking[["rank", "verdict", "score", "dcf_upside", "comps_upside", "blended_upside",
                   "signals_agree"]].to_string(formatters={
        "score": "{:.0f}".format, "dcf_upside": pct, "comps_upside": pct, "blended_upside": pct,
        "signals_agree": lambda v: "yes" if v else "NO"}))
    print(f"\nWrote {path}")


if __name__ == "__main__":
    run()
