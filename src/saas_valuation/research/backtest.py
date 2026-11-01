"""Point-in-time backtest of the engine's valuation signal (Report stage 5).

Question: on past dates, using only what was public then, did the companies the
engine called cheap go on to beat the ones it called expensive?

Point-in-time, step by step, for each rebalance date D:
  * financials: the SEC's raw companyfacts keep every filed version of every
    number with its filing date. Facts filed after D are removed and the
    pipeline's own extraction (data/edgar.py) runs on what is left, so tag
    choices, restatements and split adjustments are exactly as of D;
  * prices: the close on D (split-adjusted to today's share basis, matching
    the split-adjusted share counts);
  * beta: regressed on SPY over the two years before D; risk-free rate: the
    10-year Treasury yield (^TNX) on D;
  * comps calendar year: the last year whose 10-Ks had mostly been filed by D.
Then the engine's own comps, DCF and ranking run unchanged.

Forward returns use dividend-adjusted closes from D to D + horizon.

Run: python -m saas_valuation.research.backtest
"""

from __future__ import annotations

import copy
import json
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from saas_valuation.config import load_config, project_path, ticker_buckets, universe_tickers
from saas_valuation.data.adjustments import add_split_adjusted_shares, apply_overrides
from saas_valuation.data.edgar import extract_company
from saas_valuation.data.prices import price_history
from saas_valuation.models.comps import compute_comps
from saas_valuation.models.dcf import value_universe
from saas_valuation.models.ranking import build_ranking

# Dec-year companies file their 10-K by ~March; a date 75 days into a year
# picks the previous calendar year only once those filings exist.
FILING_LAG_DAYS = 75


@dataclass
class Inputs:
    cfg: dict
    facts: dict[str, dict]                 # ticker -> raw companyfacts JSON
    splits: dict[str, list[dict]]          # ticker -> [{date, ratio, source}]
    names: dict[str, str]
    prices: dict[str, pd.DataFrame]        # ticker -> daily close / adj_close
    spy: pd.DataFrame
    tnx: pd.DataFrame                      # 10-year Treasury yield, in percent


def load_inputs(cfg: dict | None = None, period: str = "6y") -> Inputs:
    cfg = cfg or load_config()
    raw = project_path(cfg["data"]["raw_dir"])
    tickers = universe_tickers(cfg)
    ciks = {v["ticker"]: v["cik_str"] for v in json.loads((raw / "sec" / "company_tickers.json").read_text()).values()}
    facts, splits, names, prices = {}, {}, {}, {}
    max_age = cfg["data"]["market_data_max_age_hours"]
    for t in tickers:
        facts[t] = json.loads((raw / "sec" / f"companyfacts_CIK{ciks[t]:010d}.json").read_text())
        snap = json.loads((raw / "yfinance" / f"{t}.json").read_text())
        splits[t] = [{**s, "source": "yfinance"} for s in snap.get("splits", [])]
        names[t] = snap.get("longName") or t
        prices[t] = price_history(t, raw / "yfinance", max_age, period=period)
    return Inputs(cfg, facts, splits, names, prices,
                  price_history("SPY", raw / "yfinance", max_age, period=period),
                  price_history("^TNX", raw / "yfinance", max_age, period=period))


# ---------------------------------------------------------------------------
# Point-in-time data
# ---------------------------------------------------------------------------

def facts_as_of(facts: dict, as_of: date) -> dict:
    """The companyfacts JSON with every fact filed after `as_of` removed."""
    cutoff = as_of.isoformat()
    out = {**facts, "facts": {}}
    for taxonomy, tags in facts.get("facts", {}).items():
        out["facts"][taxonomy] = {}
        for tag, body in tags.items():
            units = {u: [f for f in rows if f.get("filed", "9999") <= cutoff] for u, rows in body.get("units", {}).items()}
            units = {u: rows for u, rows in units.items() if rows}
            if units:
                out["facts"][taxonomy][tag] = {**body, "units": units}
    return out


def financials_as_of(inp: Inputs, as_of: date) -> pd.DataFrame:
    """The pipeline's financials table as it would have looked on `as_of`."""
    d = inp.cfg["data"]
    records, issues = [], []
    for t, facts in inp.facts.items():
        recs, iss = extract_company(t, facts_as_of(facts, as_of), d["annual_period_days"]["min"],
                                    d["annual_period_days"]["max"], d["years_of_history"])
        records += recs
        issues += iss
    # Hand-entered values only exist for years filed by then, so they apply only if the year exists.
    records, _ = apply_overrides(records, issues, inp.cfg.get("overrides"))
    long_df = pd.DataFrame(records)
    wide = (long_df.pivot_table(index=["ticker", "fiscal_year", "period_end"], columns="concept",
                                values="value", aggfunc="first").reset_index().rename_axis(columns=None))
    wide.insert(1, "bucket", wide["ticker"].map(ticker_buckets(inp.cfg)))
    return add_split_adjusted_shares(wide, long_df, inp.splits)


def price_on(df: pd.DataFrame, day: date, col: str = "close") -> float:
    s = df.loc[:pd.Timestamp(day), col]
    return float(s.iloc[-1]) if len(s) else np.nan


def beta_as_of(stock: pd.DataFrame, spy: pd.DataFrame, as_of: date, years: int = 2) -> float:
    start = pd.Timestamp(as_of) - pd.DateOffset(years=years)
    r = pd.concat([stock["adj_close"], spy["adj_close"]], axis=1, keys=["s", "m"]).loc[start:pd.Timestamp(as_of)]
    r = r.pct_change().dropna()
    if len(r) < 120:
        return np.nan
    return float(r["s"].cov(r["m"]) / r["m"].var())


def market_as_of(inp: Inputs, fin: pd.DataFrame, as_of: date) -> pd.DataFrame:
    rows = []
    for t in inp.facts:
        company = fin[fin["ticker"] == t].sort_values("fiscal_year")
        shares = company["diluted_shares_split_adj"].dropna()
        price = price_on(inp.prices[t], as_of)
        if shares.empty or not np.isfinite(price):
            continue
        rows.append({"ticker": t, "name": inp.names[t], "price": price, "implied_shares": float(shares.iloc[-1]),
                     "market_cap": price * float(shares.iloc[-1]),
                     "beta": beta_as_of(inp.prices[t], inp.spy, as_of), "currency": "USD",
                     "fetched_at": as_of.isoformat()})
    return pd.DataFrame(rows)


def calendar_year_for(as_of: date) -> int:
    return (as_of - timedelta(days=FILING_LAG_DAYS)).year - 1


def signals_as_of(inp: Inputs, as_of: date) -> pd.DataFrame:
    """Every signal for every company, using only what was public on `as_of`."""
    cfg = copy.deepcopy(inp.cfg)
    cfg["comps"]["calendar_year"] = calendar_year_for(as_of)
    cfg["dcf"]["valuation_date"] = as_of.isoformat()
    cfg["dcf"]["risk_free_rate"] = price_on(inp.tnx, as_of) / 100
    cfg["dcf"]["company_overrides"] = {}
    fin = financials_as_of(inp, as_of)
    market = market_as_of(inp, fin, as_of)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tables = compute_comps(fin, market, cfg["comps"])
        comps, bridge = tables["comps"], tables["ev_bridge"]
        comps = comps[comps["revenue"].notna() & comps["revenue_growth"].notna() & comps["ev_revenue"].notna()]
        mkt = market.set_index("ticker").loc[comps.index]
        summary, _, _, _ = value_universe(comps, bridge.loc[comps.index], mkt, cfg)
        ranking = build_ranking(comps, bridge.loc[comps.index], mkt, summary, cfg)["ranking"]
    out = ranking[["score", "dcf_upside", "comps_upside", "blended_upside", "verdict"]].copy()
    out["cheap_ev_revenue"] = -comps["ev_revenue"]               # naive value: lower multiple = higher signal
    out["momentum_12_1"] = [mom_12_1(inp.prices[t], as_of) for t in out.index]
    out["calendar_year"] = cfg["comps"]["calendar_year"]
    out["risk_free_rate"] = cfg["dcf"]["risk_free_rate"]
    out.insert(0, "date", as_of.isoformat())
    return out


def mom_12_1(df: pd.DataFrame, as_of: date) -> float:
    """Return from 12 months to 1 month before the date (skipping the most recent month, as is standard)."""
    end = price_on(df, (pd.Timestamp(as_of) - pd.DateOffset(months=1)).date(), "adj_close")
    start = price_on(df, (pd.Timestamp(as_of) - pd.DateOffset(months=12)).date(), "adj_close")
    return end / start - 1 if np.isfinite(start) and start > 0 else np.nan


def forward_return(df: pd.DataFrame, as_of: date, months: int) -> float:
    end_day = (pd.Timestamp(as_of) + pd.DateOffset(months=months)).date()
    if pd.Timestamp(end_day) > df.index.max():
        return np.nan
    return price_on(df, end_day, "adj_close") / price_on(df, as_of, "adj_close") - 1


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

SIGNALS = {"score": "Engine score (DCF + comps)", "dcf_upside": "DCF upside", "comps_upside": "Comps upside",
           "cheap_ev_revenue": "Baseline: low EV/revenue", "momentum_12_1": "Baseline: 12-1 month momentum"}


def newey_west_t(x: pd.Series, lags: int) -> float:
    """t-statistic of the mean with Newey-West standard errors (overlapping return windows are correlated)."""
    x = x.dropna().to_numpy()
    n = len(x)
    if n < 3:
        return np.nan
    e = x - x.mean()
    var = (e @ e) / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1 - k / (lags + 1)
        var += 2 * w * (e[k:] @ e[:-k]) / n
    return float(x.mean() / np.sqrt(var / n)) if var > 0 else np.nan


def evaluate(panel: pd.DataFrame, horizon: int, rebalance_months: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-date ICs and tercile spreads, then a summary per signal."""
    col = f"fwd_{horizon}m"
    per_date = []
    for d, g in panel.dropna(subset=[col]).groupby("date"):
        row = {"date": d, "n": len(g), "universe_return": g[col].mean()}
        for s in SIGNALS:
            gg = g.dropna(subset=[s])
            if len(gg) < 8:
                continue
            row[f"ic_{s}"] = gg[s].rank().corr(gg[col].rank())            # Spearman rank IC
            k = len(gg) // 3
            ordered = gg.sort_values(s)
            row[f"spread_{s}"] = ordered[col].iloc[-k:].mean() - ordered[col].iloc[:k].mean()
        per_date.append(row)
    per_date = pd.DataFrame(per_date)
    lags = max(horizon // rebalance_months - 1, 0)        # windows overlap when horizon > rebalance step
    summary = []
    for s, label in SIGNALS.items():
        ic, sp = per_date.get(f"ic_{s}"), per_date.get(f"spread_{s}")
        if ic is None or ic.dropna().empty:
            continue
        summary.append({"signal": label, "dates": int(ic.notna().sum()), "mean_ic": ic.mean(), "ic_sd": ic.std(),
                        "ic_t_nw": newey_west_t(ic, lags), "ic_hit_rate": (ic > 0).mean(),
                        "mean_tercile_spread": sp.mean(), "spread_t_nw": newey_west_t(sp, lags),
                        "spread_hit_rate": (sp > 0).mean()})
    return per_date, pd.DataFrame(summary).set_index("signal")


def rebalance_dates(start: date, end: date, months: int) -> list[date]:
    return [d.date() for d in pd.date_range(start, end, freq=f"{months}ME")]


def ic_by_date(panel: pd.DataFrame, signal: str, horizon: int) -> pd.Series:
    col = f"fwd_{horizon}m"
    d = panel.dropna(subset=[signal, col])
    return d.groupby("date").apply(lambda g: g[signal].rank().corr(g[col].rank()), include_groups=False)


def robustness(panel: pd.DataFrame, horizon: int = 12, signal: str = "score", permutations: int = 5000,
               seed: int = 0) -> dict:
    """The checks an investment committee would ask for before believing the headline IC."""
    ic = ic_by_date(panel, signal, horizon)
    annual = ic[[d[5:7] == ic.index[0][5:7] for d in ic.index]]          # one date per year: no overlap
    loo = pd.Series({t: ic_by_date(panel[panel["ticker"] != t], signal, horizon).mean()
                     for t in sorted(panel["ticker"].unique())})
    rng = np.random.default_rng(seed)
    d = panel.dropna(subset=[signal, f"fwd_{horizon}m"])
    null = np.empty(permutations)
    for i in range(permutations):
        shuffled = d.assign(**{signal: d.groupby("date")[signal].transform(lambda x: rng.permutation(x.to_numpy()))})
        null[i] = ic_by_date(shuffled, signal, horizon).mean()
    return {"ic_by_date": ic, "annual": annual, "leave_one_out": loo, "permutation_p": float((null >= ic.mean()).mean()),
            "null_95": float(np.quantile(null, 0.95))}


def run_backtest(start: date = date(2023, 3, 31), end: date | None = None, rebalance_months: int = 3,
                 horizons: tuple[int, ...] = (6, 12), comps_regressor: str | None = None) -> dict:
    inp = load_inputs()
    if comps_regressor:
        inp.cfg["ranking"]["comps_regressor"] = comps_regressor
    last_price = min(df.index.max() for df in inp.prices.values()).date()
    end = end or last_price
    panels = []
    for d in rebalance_dates(start, end, rebalance_months):
        sig = signals_as_of(inp, d)
        for h in horizons:
            sig[f"fwd_{h}m"] = [forward_return(inp.prices[t], d, h) for t in sig.index]
        panels.append(sig)
        print(f"{d}: CY{sig['calendar_year'].iloc[0]} comps, {len(sig)} companies, "
              f"10y yield {sig['risk_free_rate'].iloc[0]:.2%}")
    panel = pd.concat(panels).rename_axis("ticker").reset_index()
    results = {"panel": panel}
    for h in horizons:
        results[h] = evaluate(panel, h, rebalance_months)
    return results


def main() -> None:
    from saas_valuation.research.backtest_report import write_note
    cfg = load_config()
    out = project_path(cfg["data"]["processed_dir"]) / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    res = run_backtest()
    res["panel"].to_csv(out / "panel.csv", index=False)
    for h in (6, 12):
        per_date, summary = res[h]
        per_date.to_csv(out / f"per_date_{h}m.csv", index=False)
        summary.to_csv(out / f"summary_{h}m.csv")
        print(f"\n=== {h}-month forward returns ===")
        print(summary.to_string(float_format=lambda v: f"{v:,.2f}"))
    rob = robustness(res["panel"])
    alt_reg = "rule_of_40" if cfg["ranking"]["comps_regressor"] != "rule_of_40" else "revenue_growth"
    print(f"\nDesign check: rerunning with comps regressor = {alt_reg}")
    alt = run_backtest(horizons=(12,), comps_regressor=alt_reg)[12][1]
    print(f"\nNon-overlapping years: {rob['annual'].round(2).to_dict()}")
    print(f"Leave-one-company-out IC range: {rob['leave_one_out'].min():.2f} to {rob['leave_one_out'].max():.2f}")
    print(f"Permutation p-value: {rob['permutation_p']:.4f}")
    pdf = write_note(cfg, res, rob, alt, alt_reg)
    print(f"\nBacktest note: {pdf}")


if __name__ == "__main__":
    main()
