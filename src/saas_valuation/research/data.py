"""Everything one equity research report needs, gathered from the engine's models.

Nothing is re-valued here: the DCF, comps regression, price targets and risk
summaries come from models/, so a report always matches the Excel outputs.
The only derived series is fiscal-year free cash flow for the history charts,
computed with the same definition the comps use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from saas_valuation.config import load_config, project_path
from saas_valuation.data.prices import price_history
from saas_valuation.models.comps import prepare_fiscal
from saas_valuation.models.dcf import value_universe
from saas_valuation.models.ranking import build_ranking
from saas_valuation.models.target import price_targets


@dataclass
class CompanyReport:
    ticker: str
    name: str
    bucket: str
    fiscal: pd.DataFrame            # one row per fiscal year: revenue, margins, FCF, SBC...
    comps_row: pd.Series            # calendar-year comps metrics
    dcf: pd.Series                  # dcf summary row
    projection: pd.DataFrame        # DCF projection by year (explicit + fade)
    sensitivity: pd.DataFrame       # value per share by WACC x terminal growth
    target: pd.Series               # price target, scenarios, rating
    prices: pd.DataFrame            # daily closes, ~2 years
    risk: dict | None = None        # Stage 7 AI risk summary, if it has been run
    peers: pd.DataFrame = field(default_factory=pd.DataFrame)   # comps of the whole universe
    regression: dict = field(default_factory=dict)
    valuation_date: date | None = None

    @property
    def week52(self) -> tuple[float, float]:
        last = self.prices[self.prices.index >= self.prices.index.max() - pd.Timedelta(days=365)]["close"]
        return float(last.min()), float(last.max())


@dataclass
class Universe:
    cfg: dict
    comps: pd.DataFrame
    bridge: pd.DataFrame
    market: pd.DataFrame
    fiscal: pd.DataFrame
    dcf_summary: pd.DataFrame
    dcf_results: dict
    targets: pd.DataFrame
    regression: dict
    valuation_date: date

    def company(self, ticker: str) -> CompanyReport:
        d = self.cfg["data"]
        cache = project_path(d["raw_dir"]) / "yfinance"
        risk_file = project_path(d["processed_dir"]) / "risk" / f"{ticker}.json"
        risk = json.loads(risk_file.read_text()) if risk_file.exists() else None
        res = self.dcf_results[ticker]
        return CompanyReport(
            ticker=ticker, name=self.comps.at[ticker, "name"], bucket=self.comps.at[ticker, "bucket"],
            fiscal=self.fiscal[self.fiscal["ticker"] == ticker].set_index("fiscal_year"),
            comps_row=self.comps.loc[ticker], dcf=self.dcf_summary.loc[ticker],
            projection=res["projection"], sensitivity=res["sensitivity"], target=self.targets.loc[ticker],
            prices=price_history(ticker, cache, d["market_data_max_age_hours"]),
            risk=risk if risk and risk.get("status") == "ok" else None,
            peers=self.comps, regression=self.regression, valuation_date=self.valuation_date)


def fiscal_history(financials: pd.DataFrame, subtract_capitalized_software: bool) -> pd.DataFrame:
    fin = prepare_fiscal(financials).sort_values(["ticker", "fiscal_year"])
    software = fin["capitalized_software"] if subtract_capitalized_software else 0.0
    fin["fcf"] = fin["operating_cash_flow"] - fin["capex"] - software
    fin["fcf_after_sbc"] = fin["fcf"] - fin["stock_based_comp"]

    def ratio(num):
        return (num / fin["revenue"]).where(fin["revenue"] > 0)

    fin["gross_margin"] = ratio(fin["gross_profit_calc"])
    fin["operating_margin"] = ratio(fin["operating_income"])
    fin["fcf_margin"] = ratio(fin["fcf"])
    fin["sbc_pct_revenue"] = ratio(fin["stock_based_comp"])
    prev = fin.groupby("ticker")[["revenue", "fiscal_year"]].shift(1)
    fin["revenue_growth"] = (fin["revenue"] / prev["revenue"] - 1).where(fin["fiscal_year"] - prev["fiscal_year"] == 1)
    return fin


def load_universe(cfg: dict | None = None) -> Universe:
    cfg = cfg or load_config()
    processed = project_path(cfg["data"]["processed_dir"])
    comps, bridge, market = (pd.read_csv(processed / f"{n}.csv", index_col="ticker")
                             for n in ("comps", "ev_bridge", "market_data"))
    financials = pd.read_csv(processed / "financials.csv")
    summary, results, valuation_date, _ = value_universe(comps, bridge, market, cfg)
    regression = build_ranking(comps, bridge, market, summary, cfg)["regression"]
    targets = price_targets(comps, bridge, market, cfg)["targets"]
    fiscal = fiscal_history(financials, cfg["comps"]["fcf_subtract_capitalized_software"])
    return Universe(cfg, comps, bridge, market, fiscal, summary, results, targets, regression,
                    valuation_date if isinstance(valuation_date, date) else date.fromisoformat(str(valuation_date)))


def finite(x) -> bool:
    return x is not None and isinstance(x, (int, float, np.floating)) and np.isfinite(x)
