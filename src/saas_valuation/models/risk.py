"""Stage 7 (optional): AI summaries of each company's 10-K risk factors.

Why: the comps and DCF only see the numbers. Stage 5's top-ranked names
are mostly seat-based application software (HubSpot, Adobe, Workday,
DocuSign), and the market may be discounting them for AI disruption that
hasn't reached the financials yet. Each company's own risk disclosures
are a way to test that.

Claude reads Item 1A and returns structured JSON (enforced by a JSON
schema): a summary, the top risks ranked by materiality, and a specific
assessment of AI disruption. Every quote is then checked against the
filing text, so a paraphrase can't pass as a quote.

Run from the project root:
    python -m saas_valuation.models.risk --estimate          # token count + cost, no generation
    python -m saas_valuation.models.risk --tickers HUBS,ADBE # run a subset
    python -m saas_valuation.models.risk                     # run all (asks to confirm the cost)

Results are cached per filing, so re-running only processes new 10-Ks.
"""

import argparse
import json
import re
import sys
from datetime import datetime

import anthropic
import pandas as pd
from dotenv import load_dotenv

from saas_valuation.config import (PROJECT_ROOT, load_config, project_path, sec_user_agent,
                                   universe_tickers)
from saas_valuation.data.filings import fetch_risk_factors
from saas_valuation.data.sec_client import SecClient

CATEGORIES = ["competition", "ai_disruption", "demand_macro", "execution_growth", "security_privacy",
              "regulatory_legal", "platform_dependency", "international_fx", "capital_debt", "people",
              "accounting_controls", "other"]
AI_MECHANISMS = ["seat_compression", "ai_native_competition", "pricing_pressure", "product_obsolescence",
                 "ai_investment_cost", "ai_regulation", "none"]

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "top_risks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "category": {"type": "string", "enum": CATEGORIES},
                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                "company_specific": {"type": "boolean"},
                "explanation": {"type": "string"},
                "quote": {"type": "string"},
            },
            "required": ["title", "category", "severity", "company_specific", "explanation", "quote"],
            "additionalProperties": False,
        }},
        "ai_disruption": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["high", "medium", "low", "none"]},
                "mechanisms": {"type": "array", "items": {"type": "string", "enum": AI_MECHANISMS}},
                "assessment": {"type": "string"},
                "quote": {"type": "string"},
            },
            "required": ["level", "mechanisms", "assessment", "quote"],
            "additionalProperties": False,
        },
    },
    "required": ["summary", "top_risks", "ai_disruption"],
    "additionalProperties": False,
}

SYSTEM = """You are an equity research analyst covering US software companies. You'll be given \
the Risk Factors section (Item 1A) of a company's latest 10-K. Your reader is building a \
valuation model and wants to know which risks actually matter for this business's future \
cash flows.

Item 1A is written by lawyers: most of it is generic, and the order is not a ranking. So:
- Rank risks by how much they could change revenue growth, margins or the company's long-run \
position, not by where they appear or how long they are.
- Mark a risk company_specific only if it's about this company's particular business model, \
customers, products or competitive position. Risks every software company lists (a generic \
security breach, key personnel, stock price volatility) are not company-specific, and should \
only make the top list if this filing gives a concrete, unusual reason.
- Explanations should be plain English, 1-3 sentences, and say why the risk matters for value.

For ai_disruption, judge only what the filing itself says about AI as a threat to this \
company's business. Mechanisms to look for: customers needing fewer seats or licenses because \
AI does the work (seat_compression), AI-native competitors (ai_native_competition), pressure \
to price AI features lower or bundle them (pricing_pressure), core products becoming less \
necessary (product_obsolescence), heavy AI spending that may not pay off or hurts margins \
(ai_investment_cost), and AI regulation (ai_regulation). AI described only as an opportunity \
is not disruption. Use level "none" with mechanisms ["none"] if the filing doesn't discuss AI \
as a threat. Calibrate level by how directly the filing ties AI to its core revenue model.

Every quote must be copied verbatim from the provided text, at most 30 words, with no \
ellipses or edits. Quotes are checked against the filing automatically."""


def build_request(company: str, ticker: str, filing: dict, cfg: dict) -> dict:
    """The Messages API parameters for one company (shared by count_tokens and the real call)."""
    a = cfg["ai_risk"]
    user = (f"Company: {company} ({ticker})\n10-K filed {filing['filing_date']}, fiscal year ended "
            f"{filing['report_date']}.\n\nIdentify the {a['top_risks']} most material risks, then "
            f"assess AI disruption risk.\n\n<risk_factors>\n{filing['text']}\n</risk_factors>")
    return {"model": a["model"], "system": SYSTEM, "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": a["effort"], "format": {"type": "json_schema", "schema": SCHEMA}}}


def _normalize(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-")
    s = re.sub(r"[^a-z0-9$%'\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, source: str) -> bool:
    """Is the quote verbatim in the source, ignoring case, punctuation and whitespace?"""
    q = _normalize(quote)
    return bool(q) and q in _normalize(source)


def cost(usage, a: dict) -> float:
    return (usage["input_tokens"] * a["price_input_per_mtok"]
            + usage["output_tokens"] * a["price_output_per_mtok"]) / 1e6


def summarize(client: anthropic.Anthropic, company: str, ticker: str, filing: dict, cfg: dict) -> dict:
    a = cfg["ai_risk"]
    request = build_request(company, ticker, filing, cfg)
    # Streaming avoids HTTP timeouts on long inputs. fallbacks="default": if the
    # model declines, the API retries on Anthropic's recommended fallback model.
    with client.beta.messages.stream(max_tokens=a["max_tokens"], betas=["server-side-fallback-2026-07-01"],
                                     fallbacks="default", **request) as stream:
        message = stream.get_final_message()

    usage = {"input_tokens": message.usage.input_tokens, "output_tokens": message.usage.output_tokens}
    meta = {"ticker": ticker, "company": company, "accession": filing["accession"],
            "filing_date": filing["filing_date"], "report_date": filing["report_date"], "url": filing["url"],
            "words": filing["words"], "model_requested": a["model"], "model_served": message.model,
            "stop_reason": message.stop_reason, "usage": usage, "cost_usd": cost(usage, a),
            "generated_at": datetime.now().isoformat(timespec="seconds")}

    if message.stop_reason == "refusal":
        return {**meta, "status": "refused", "detail": str(getattr(message, "stop_details", "") or "")}
    if message.stop_reason == "max_tokens":
        return {**meta, "status": "truncated", "detail": "Hit max_tokens; raise ai_risk.max_tokens."}

    text = next(b.text for b in message.content if b.type == "text")
    result = json.loads(text)
    for risk in result["top_risks"]:
        risk["quote_verified"] = quote_found(risk["quote"], filing["text"])
    ai = result["ai_disruption"]
    ai["quote_verified"] = ai["level"] == "none" or quote_found(ai["quote"], filing["text"])
    return {**meta, "status": "ok", **result}


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def summary_table(results: list[dict], ranking: pd.DataFrame | None) -> pd.DataFrame:
    rows = []
    for r in results:
        if r.get("status") != "ok":
            continue
        ai = r["ai_disruption"]
        rows.append({"ticker": r["ticker"], "ai_level": ai["level"],
                     "ai_mechanisms": ", ".join(m for m in ai["mechanisms"] if m != "none"),
                     "ai_assessment": ai["assessment"], "ai_quote": ai["quote"],
                     "ai_quote_verified": ai["quote_verified"], "summary": r["summary"],
                     "top_risk": r["top_risks"][0]["title"] if r["top_risks"] else "",
                     "quotes_verified": sum(k["quote_verified"] for k in r["top_risks"]),
                     "filing_date": r["filing_date"], "url": r["url"]})
    table = pd.DataFrame(rows).set_index("ticker")
    if ranking is not None and not table.empty:
        table = ranking[["rank", "name", "verdict", "blended_upside"]].join(table, how="inner").sort_values("rank")
    return table


def run(tickers: list[str] | None, estimate_only: bool, assume_yes: bool, refresh: bool) -> None:
    from saas_valuation.reports.risk_excel import write_risk_workbook

    load_dotenv(PROJECT_ROOT / ".env")
    cfg = load_config()
    d, a = cfg["data"], cfg["ai_risk"]
    tickers = tickers or universe_tickers(cfg)
    out_dir = project_path(d["processed_dir"]) / "risk"
    out_dir.mkdir(parents=True, exist_ok=True)

    sec = SecClient(sec_user_agent(cfg), project_path(d["raw_dir"]) / "sec",
                    d["max_requests_per_second"], d["sec_cache_max_age_days"])
    ciks = sec.ticker_to_cik()
    names = pd.read_csv(project_path(d["processed_dir"]) / "market_data.csv", index_col="ticker")["name"]

    filings, todo = {}, []
    for t in tickers:
        filing = fetch_risk_factors(sec, t, ciks[t], project_path(d["raw_dir"]) / "10k")
        if filing["status"] == "error":
            print(f"  {t:5} skipped: {filing['detail']}")
            continue
        filings[t] = filing
        cached = out_dir / f"{t}.json"
        previous = json.loads(cached.read_text()) if cached.exists() else {}
        if refresh or previous.get("accession") != filing["accession"] or previous.get("status") != "ok":
            todo.append(t)

    try:
        client = anthropic.Anthropic()
        counted = {t: client.messages.count_tokens(**build_request(names.get(t, t), t, filings[t], cfg)).input_tokens
                   for t in todo}
        basis = "exact (count_tokens API)"
    except (anthropic.AuthenticationError, TypeError) as exc:
        if not estimate_only:
            sys.exit(f"No usable Anthropic credentials ({type(exc).__name__}). Add ANTHROPIC_API_KEY to .env.")
        counted = {t: round(len(filings[t]["text"]) / 4) + 1500 for t in todo}  # ~4 chars/token + prompt
        basis = "approximate (~4 characters per token; no API key for an exact count)"

    total_in = sum(counted.values())
    total_out = len(todo) * a["estimated_output_tokens"]
    est = (total_in * a["price_input_per_mtok"] + total_out * a["price_output_per_mtok"]) / 1e6
    print(f"\n{len(todo)} of {len(filings)} filings need summarizing ({len(filings) - len(todo)} cached).")
    print(f"Input tokens: {total_in:,} [{basis}]")
    print(f"Estimated cost with {a['model']}: ${est:.2f} "
          f"(input ${total_in * a['price_input_per_mtok'] / 1e6:.2f} + output allowance "
          f"${total_out * a['price_output_per_mtok'] / 1e6:.2f})")
    if estimate_only:
        return
    if todo and not assume_yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return

    spent = 0.0
    for t in todo:
        try:
            result = summarize(client, names.get(t, t), t, filings[t], cfg)
        except anthropic.APIStatusError as exc:
            print(f"  {t:5} API error {exc.status_code}: {exc.message}")
            continue
        except anthropic.APIConnectionError:
            print(f"  {t:5} network error; re-run to retry")
            continue
        (out_dir / f"{t}.json").write_text(json.dumps(result, indent=2))
        spent += result["cost_usd"]
        detail = (f"AI risk {result['ai_disruption']['level']}" if result["status"] == "ok"
                  else result["status"])
        print(f"  {t:5} {detail:16} ${result['cost_usd']:.2f}  (served by {result['model_served']})")
    if todo:
        print(f"Spent ${spent:.2f}")

    results = [json.loads((out_dir / f"{t}.json").read_text()) for t in filings if (out_dir / f"{t}.json").exists()]
    ranking_file = project_path(d["processed_dir"]) / "ranking.csv"
    ranking = pd.read_csv(ranking_file, index_col=0) if ranking_file.exists() else None
    table = summary_table(results, ranking)
    table.to_csv(project_path(d["processed_dir"]) / "risk_summary.csv")
    path = project_path(cfg["comps"]["output_dir"]) / "risk_summaries.xlsx"
    write_risk_workbook(path, table, results)

    if ranking is not None and not table.empty:
        print("\nAI disruption risk (from the 10-K) vs. valuation verdict:")
        print(pd.crosstab(table["verdict"], table["ai_level"]).reindex(
            columns=[c for c in ["high", "medium", "low", "none"] if c in set(table["ai_level"])]).to_string())
    print(f"\nWrote {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Summarize 10-K risk factors with Claude.")
    parser.add_argument("--estimate", action="store_true", help="count tokens and estimate cost only")
    parser.add_argument("--tickers", help="comma-separated subset, e.g. HUBS,ADBE")
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    parser.add_argument("--refresh", action="store_true", help="re-summarize even if cached")
    args = parser.parse_args()
    run(args.tickers.split(",") if args.tickers else None, args.estimate, args.yes, args.refresh)
