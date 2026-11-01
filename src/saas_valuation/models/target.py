"""12-month price targets, bull/bear scenarios and ratings (Report stage 1).

Builds on the existing models instead of re-implementing them:
  * fair value today = blend of the DCF value (models.dcf) and the comps-implied
    price from the growth regression (models.ranking);
  * price target = fair value rolled forward over the horizon at the cost of
    equity minus dividend yield, rounded like a published target;
  * bull / bear = the same blend with the DCF's starting growth and mature
    margin shifted, and the comps' fair multiple moved by a number of standard
    deviations of the regression residuals;
  * rating = Overweight / Neutral / Underweight on the expected return, relative
    to the coverage universe by default.

Run: python -m saas_valuation.models.target
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from saas_valuation.config import load_config, project_path
from saas_valuation.models.dcf import value_universe
from saas_valuation.models.ranking import build_ranking, fair_multiples, fit_log_multiple


def residual_sd(x: pd.Series, multiple: pd.Series) -> float:
    """Standard deviation of ln(multiple) around the fitted line (degrees of freedom n-2)."""
    valid = x.notna() & multiple.notna() & (multiple > 0)
    a, b, _ = fit_log_multiple(x[valid], multiple[valid])
    resid = np.log(multiple[valid]) - (a + b * x[valid])
    return float(np.sqrt((resid ** 2).sum() / (valid.sum() - 2)))


def comps_price(fair_multiple: pd.Series, comps: pd.DataFrame, bridge: pd.DataFrame,
                market: pd.DataFrame) -> pd.Series:
    """Share price implied by a fair EV/revenue multiple (same bridge as models.ranking)."""
    net_cash = bridge["cash"] + bridge["short_term_investments"] - bridge["debt"]
    return (fair_multiple * comps["revenue"] + net_cash) / market["implied_shares"]


def blend(dcf: pd.Series, comps: pd.Series, w_dcf: float, w_comps: float) -> tuple[pd.Series, pd.Series]:
    """Weighted fair value; where the DCF isn't meaningful, comps alone. Returns (value, basis)."""
    both = dcf.notna() & (dcf > 0)
    value = ((dcf * w_dcf + comps * w_comps) / (w_dcf + w_comps)).where(both, comps)
    basis = pd.Series(np.where(both, "DCF + comps", "comps only (DCF not meaningful)"), index=dcf.index)
    return value, basis


def roll_forward(value: pd.Series, cost_of_equity: pd.Series, horizon_months: int, dividend_yield: float) -> pd.Series:
    return value * (1 + cost_of_equity - dividend_yield) ** (horizon_months / 12)


def round_target(value: pd.Series, step: float) -> pd.Series:
    return (value / step).round() * step


def rate(expected_return: pd.Series, rating_cfg: dict) -> tuple[pd.Series, float]:
    """Ratings and the reference return they are measured against."""
    ref = float(expected_return.median()) if rating_cfg["basis"] == "relative" else 0.0
    band, labels = rating_cfg["band"], rating_cfg["labels"]
    rel = expected_return - ref
    out = pd.Series(labels["mid"], index=expected_return.index)
    out[rel >= band] = labels["up"]
    out[rel <= -band] = labels["down"]
    return out, ref


def scenario_cfg(cfg: dict, base: pd.DataFrame, s: dict) -> dict:
    """Config whose per-company overrides shift growth and mature margin from the base case."""
    out = copy.deepcopy(cfg)
    out["dcf"]["company_overrides"] = {
        t: {"starting_growth": base.at[t, "start_growth"] + s["starting_growth_delta"],
            "target_fcf_margin": max(base.at[t, "target_fcf_margin"] + s["target_fcf_margin_delta"], 0.0)}
        for t in base.index}
    return out


def price_targets(comps: pd.DataFrame, bridge: pd.DataFrame, market: pd.DataFrame, cfg: dict) -> dict:
    rep = cfg["report"]
    pt, sc = rep["price_target"], rep["scenarios"]
    w_dcf, w_comps = pt["dcf_weight"], pt["comps_weight"]

    base, _, valuation_date, _ = value_universe(comps, bridge, market, cfg)
    ranking = build_ranking(comps, bridge, market, base, cfg)["ranking"]
    x = comps[cfg["ranking"]["comps_regressor"]]
    fair = fair_multiples(x, comps["ev_revenue"], cfg["ranking"]["leave_one_out"])
    sd = residual_sd(x, comps["ev_revenue"])
    price, k_e = market["price"], base["cost_of_equity"]

    def case(dcf_value: pd.Series, multiple_shift_sd: float):
        cp = comps_price(fair * np.exp(multiple_shift_sd * sd), comps, bridge, market)
        value, basis = blend(dcf_value, cp, w_dcf, w_comps)
        target = round_target(roll_forward(value, k_e, pt["horizon_months"], pt["dividend_yield"]), pt["round_to"])
        return cp, value, basis, target

    comps_base, fair_value, basis, target = case(base["implied_price"], 0.0)
    out = pd.DataFrame({
        "name": comps["name"], "bucket": comps["bucket"], "price": price,
        "dcf_value": base["implied_price"], "comps_value": comps_base, "fair_value": fair_value,
        "basis": basis, "cost_of_equity": k_e, "price_target": target,
    })
    for name in ("bear", "bull"):
        s = sc[name]
        scen, _, _, _ = value_universe(comps, bridge, market, scenario_cfg(cfg, base, s))
        cp, value, _, tgt = case(scen["implied_price"], s["comps_residual_sd"])
        out[f"{name}_dcf"], out[f"{name}_comps"], out[f"{name}_target"] = scen["implied_price"], cp, tgt
    # Scenario targets bracket the base by construction; guard against odd blends anyway.
    out["bear_target"] = out[["bear_target", "price_target"]].min(axis=1)
    out["bull_target"] = out[["bull_target", "price_target"]].max(axis=1)

    out["expected_return"] = out["price_target"] / price - 1
    out["bear_return"] = out["bear_target"] / price - 1
    out["bull_return"] = out["bull_target"] / price - 1
    out["rating"], reference = rate(out["expected_return"], rep["rating"])
    out["outlier"] = out["expected_return"].abs() > pt["flag_return_beyond"]
    out["ranking_verdict"] = ranking["verdict"]
    out = out.sort_values("expected_return", ascending=False)
    return {"targets": out, "residual_sd": sd, "reference_return": reference, "valuation_date": valuation_date}


def run() -> None:
    cfg = load_config()
    processed = project_path(cfg["data"]["processed_dir"])
    comps, bridge, market = (pd.read_csv(processed / f"{n}.csv", index_col="ticker")
                             for n in ("comps", "ev_bridge", "market_data"))
    res = price_targets(comps, bridge, market, cfg)
    t = res["targets"]
    t.to_csv(processed / "price_targets.csv")
    r = cfg["report"]["rating"]
    print(f"Valued as of {res['valuation_date']}. Comps residual s.d. {res['residual_sd']:.2f} "
          f"(fair multiple x{np.exp(res['residual_sd']):.2f} in the bull case, /{np.exp(res['residual_sd']):.2f} bear).")
    print(f"Rating basis: {r['basis']}, band ±{r['band']:.0%} around {res['reference_return']:+.0%}\n")
    money, pct = "${:,.0f}".format, "{:+.0%}".format
    print(t[["price", "bear_target", "price_target", "bull_target", "expected_return", "rating",
             "ranking_verdict", "basis"]].to_string(formatters={
        "price": "${:,.2f}".format, "bear_target": money, "price_target": money, "bull_target": money,
        "expected_return": pct}))
    print("\n" + t["rating"].value_counts().to_string())
    flagged = t.index[t["outlier"]].tolist()
    if flagged:
        print(f"\nOutlier targets (beyond ±{cfg['report']['price_target']['flag_return_beyond']:.0%}), "
              f"flagged in reports: {', '.join(flagged)}")


if __name__ == "__main__":
    run()
