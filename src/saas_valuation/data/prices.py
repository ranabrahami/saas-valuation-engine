"""Current market data from yfinance, cached per ticker.

EDGAR tells us what a company earned. The market tells us what investors
pay for it today. Valuation multiples need both: e.g. EV/Revenue uses a
price-based numerator (market cap -> EV) and a filing-based denominator.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

# yfinance field -> our column name
FIELDS = {
    "longName": "name",
    "currentPrice": "price",
    "marketCap": "market_cap",
    "sharesOutstanding": "shares_outstanding",
    "beta": "beta",
    "enterpriseValue": "yf_enterprise_value",  # kept only to cross-check our own EV in Stage 3
    "currency": "currency",
}


def _load_snapshot(ticker: str, cache_dir: Path, max_age_hours: float, refresh: bool) -> dict:
    cache_file = cache_dir / f"{ticker}.json"
    if not refresh and cache_file.exists():
        snapshot = json.loads(cache_file.read_text())
        age = datetime.now() - datetime.fromisoformat(snapshot["fetched_at"])
        if age < timedelta(hours=max_age_hours) and "splits" in snapshot:
            return snapshot

    yf_ticker = yf.Ticker(ticker)
    info = yf_ticker.info
    snapshot = {"fetched_at": datetime.now().isoformat(timespec="seconds"),
                **{f: info.get(f) for f in FIELDS},
                "regularMarketPrice": info.get("regularMarketPrice"),
                "splits": [{"date": ts.date().isoformat(), "ratio": float(ratio)}
                           for ts, ratio in yf_ticker.splits.items()]}
    cache_file.write_text(json.dumps(snapshot, indent=2))
    return snapshot


def fetch_market_data(ticker: str, cache_dir: Path, max_age_hours: float,
                      refresh: bool = False) -> tuple[dict, list[dict], list[dict]]:
    """Returns (market row, issues, stock splits as [{date, ratio, source}])."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    snapshot = _load_snapshot(ticker, cache_dir, max_age_hours, refresh)

    row = {"ticker": ticker, **{col: snapshot.get(f) for f, col in FIELDS.items()},
           "fetched_at": snapshot["fetched_at"]}
    if row["price"] is None:
        row["price"] = snapshot.get("regularMarketPrice")
    # yfinance's market cap counts every share class, but its sharesOutstanding
    # field sometimes covers only one (Atlassian, Workday). Backing the count out
    # of market cap gives a figure that matches what the market cap is built on.
    row["implied_shares"] = (row["market_cap"] / row["price"]
                             if row["market_cap"] and row["price"] else None)

    issues = []
    for col in ("price", "market_cap", "shares_outstanding"):
        if row[col] is None:
            issues.append({"ticker": ticker, "severity": "error", "concept": col, "fiscal_year": None,
                           "issue": "missing_market_data", "detail": f"yfinance returned no {col}."})
    if row["beta"] is None:
        issues.append({"ticker": ticker, "severity": "warning", "concept": "beta", "fiscal_year": None,
                       "issue": "missing_market_data",
                       "detail": "No beta from yfinance; DCF will fall back to dcf.default_beta."})
    if row["currency"] not in (None, "USD"):
        issues.append({"ticker": ticker, "severity": "error", "concept": "currency", "fiscal_year": None,
                       "issue": "non_usd_quote", "detail": f"Quoted in {row['currency']}."})
    splits = [{**s, "source": "yfinance"} for s in snapshot["splits"]]
    return row, issues, splits


def price_history(ticker: str, cache_dir: Path, max_age_hours: float, period: str = "2y",
                  refresh: bool = False) -> pd.DataFrame:
    """Daily prices, cached as CSV. Columns:
      close      split-adjusted, not dividend-adjusted (what the market showed, on today's share basis)
      adj_close  also dividend-adjusted, for total returns in the backtest

    yfinance restates history for splits, so CrowdStrike's 2026 4-for-1 split
    doesn't show up as a 75% drop. Works for indices too (^TNX, SPY).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe = ticker.replace("^", "_")
    cache_file = cache_dir / (f"{safe}_history.csv" if period == "2y" else f"{safe}_history_{period}.csv")
    if not refresh and cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        cached = pd.read_csv(cache_file, index_col="date", parse_dates=["date"])
        if age_hours <= max_age_hours and "adj_close" in cached:
            return cached
    hist = yf.Ticker(ticker).history(period=period, auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"yfinance returned no price history for {ticker}")
    out = pd.DataFrame({"close": hist["Close"].to_numpy(), "adj_close": hist["Adj Close"].to_numpy()},
                       index=pd.DatetimeIndex(hist.index.tz_localize(None).normalize(), name="date"))
    out.to_csv(cache_file)
    return out
