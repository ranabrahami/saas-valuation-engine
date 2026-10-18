"""Stage 2 entry point: download, clean, cache, and quality-check all data.

Run from the project root:
    python -m saas_valuation.data.pipeline            # use cache where fresh
    python -m saas_valuation.data.pipeline --refresh  # force re-download

Outputs (data/processed/):
    financials_long.csv      one row per ticker/year/concept, with source tag
    financials.csv           wide: one row per ticker/year, one column per concept
    market_data.csv          current price, market cap, shares, beta
    data_quality_report.csv  every issue found, by severity
"""

import argparse
from datetime import date

import pandas as pd

from saas_valuation.config import (load_config, project_path, sec_user_agent,
                                   ticker_buckets, universe_tickers)
from saas_valuation.data.edgar import cover_page_shares, extract_company
from saas_valuation.data.prices import fetch_market_data
from saas_valuation.data.sec_client import SecClient

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def share_count_checks(financials: pd.DataFrame, market: pd.DataFrame, tolerance: float) -> list[dict]:
    """Cross-check the share count, which every per-share number depends on.

    Uses implied shares (market cap / price) as the reference and compares it to:
      * yfinance's own share field      -> catches share classes left out
      * the SEC cover-page count        -> independent confirmation from a filing
      * the last 10-K diluted count     -> catches splits, buybacks, issuance since then
    """
    def issue(ticker, severity, name, detail):
        return {"ticker": ticker, "severity": severity, "concept": "shares",
                "fiscal_year": None, "issue": name, "detail": detail}

    issues = []
    latest_diluted = (financials.dropna(subset=["diluted_shares"])
                      .sort_values("fiscal_year").groupby("ticker").tail(1)
                      .set_index("ticker")["diluted_shares"])

    for _, row in market.iterrows():
        t, implied = row["ticker"], row["implied_shares"]
        if pd.isna(implied):
            continue

        yf_shares = row["shares_outstanding"]
        if pd.notna(yf_shares) and abs(yf_shares / implied - 1) > tolerance:
            issues.append(issue(t, "warning", "share_class_mismatch",
                                f"yfinance shares {yf_shares:,.0f} vs market cap / price {implied:,.0f}. "
                                "yfinance likely counts only one share class; use the implied count."))

        cover = row.get("sec_cover_shares")
        if pd.notna(cover) and abs(cover / implied - 1) > tolerance:
            issues.append(issue(t, "warning", "sec_cover_mismatch",
                                f"SEC cover page {cover:,.0f} ({row['sec_cover_date']}) vs "
                                f"market cap / price {implied:,.0f}."))

        filed = latest_diluted.get(t)
        if filed:
            ratio = implied / filed
            if ratio >= 1.8 and abs(ratio - round(ratio)) < 0.15:
                issues.append(issue(t, "warning", "likely_stock_split",
                                    f"Current shares are {ratio:.2f}x the last 10-K diluted count "
                                    f"({filed:,.0f}): likely a {round(ratio)}-for-1 split. Per-share "
                                    "figures from older filings must be split-adjusted."))
            elif abs(ratio - 1) > tolerance:
                issues.append(issue(t, "info", "share_count_changed",
                                    f"{ratio - 1:+.0%} vs last 10-K diluted count ({filed:,.0f}). "
                                    "Buybacks or issuance since the fiscal year ended."))
    return issues


def run(refresh: bool = False) -> None:
    cfg = load_config()
    data_cfg = cfg["data"]
    tickers = universe_tickers(cfg)
    raw_dir = project_path(data_cfg["raw_dir"])
    out_dir = project_path(data_cfg["processed_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    client = SecClient(sec_user_agent(cfg), raw_dir / "sec",
                       data_cfg["max_requests_per_second"], data_cfg["sec_cache_max_age_days"])
    cik_map = client.ticker_to_cik(refresh)

    records, issues, market_rows = [], [], []
    period = data_cfg["annual_period_days"]

    for ticker in tickers:
        cik = cik_map.get(ticker)
        if cik is None:
            issues.append({"ticker": ticker, "severity": "error", "concept": "", "fiscal_year": None,
                           "issue": "unknown_ticker", "detail": "Ticker not in SEC ticker map."})
            print(f"  {ticker:5} ✗ not found in SEC ticker map")
            continue

        try:
            facts = client.company_facts(cik, refresh)
            company_records, company_issues = extract_company(
                ticker, facts, period["min"], period["max"], data_cfg["years_of_history"])
        except Exception as exc:
            issues.append({"ticker": ticker, "severity": "error", "concept": "", "fiscal_year": None,
                           "issue": "sec_fetch_failed", "detail": str(exc)})
            print(f"  {ticker:5} ✗ SEC fetch failed: {exc}")
            continue
        records += company_records
        issues += company_issues

        try:
            market_row, market_issues = fetch_market_data(
                ticker, raw_dir / "yfinance", data_cfg["market_data_max_age_hours"], refresh)
            cover_shares, cover_date = cover_page_shares(facts)
            # A stale cover count (e.g. a dual-class filer last tagged in 2018) is worse than none.
            if cover_date and (date.today() - date.fromisoformat(cover_date)).days > 400:
                cover_shares, cover_date = None, None
            market_row.update(sec_cover_shares=cover_shares, sec_cover_date=cover_date)
            market_rows.append(market_row)
            issues += market_issues
        except Exception as exc:
            issues.append({"ticker": ticker, "severity": "error", "concept": "", "fiscal_year": None,
                           "issue": "market_fetch_failed", "detail": str(exc)})

        years = sorted({r["fiscal_year"] for r in company_records})
        span = f"FY{years[0]}–FY{years[-1]}" if years else "no years"
        print(f"  {ticker:5} ✓ {span}, {len(company_records)} data points")

    long_df = pd.DataFrame(records)
    wide_df = (long_df.pivot_table(index=["ticker", "fiscal_year", "period_end"], columns="concept",
                                   values="value", aggfunc="first")
               .reset_index().rename_axis(columns=None))
    wide_df.insert(1, "bucket", wide_df["ticker"].map(ticker_buckets(cfg)))
    market_df = pd.DataFrame(market_rows)

    issues += share_count_checks(wide_df, market_df, data_cfg["share_count_tolerance"])
    issues_df = pd.DataFrame(issues)
    if not issues_df.empty:
        issues_df = (issues_df.assign(_order=issues_df["severity"].map(SEVERITY_ORDER))
                     .sort_values(["_order", "ticker", "concept", "fiscal_year"])
                     .drop(columns="_order"))

    long_df.to_csv(out_dir / "financials_long.csv", index=False)
    wide_df.to_csv(out_dir / "financials.csv", index=False)
    market_df.to_csv(out_dir / "market_data.csv", index=False)
    issues_df.to_csv(out_dir / "data_quality_report.csv", index=False)

    counts = issues_df["severity"].value_counts() if not issues_df.empty else {}
    print(f"\nWrote {len(wide_df)} company-years to {out_dir}")
    print("Data quality: " + ", ".join(f"{counts.get(s, 0)} {s}s" for s in SEVERITY_ORDER))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="ignore cache and re-download")
    run(parser.parse_args().refresh)
