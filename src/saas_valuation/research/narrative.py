"""The written sections of an equity research report, drafted by Claude (Report stage 3).

Claude explains the model's view; it does not form one. The rating and price
target come from models/target.py and are given to it as facts. Every number it
writes must come from the fact sheet (tagged [F7]), the 10-K excerpt or the
risk summary, and is checked by research/grounding.py. A draft with an
unverified number gets one retry naming the problem; if it still fails, the
result is saved with status "needs_review" and the report won't use it.

Run:
    python -m saas_valuation.research.narrative --estimate          # token count and cost, free
    python -m saas_valuation.research.narrative --tickers ADBE      # one company
    python -m saas_valuation.research.narrative                     # all, asks before spending
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from saas_valuation.config import PROJECT_ROOT, load_config, project_path
from saas_valuation.data.filings import business_section
from saas_valuation.research import grounding
from saas_valuation.research.data import CompanyReport, finite, load_universe

SECTIONS = ["headline", "summary", "thesis", "business_overview", "valuation", "risks", "what_would_change"]

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": SECTIONS,
    "properties": {
        "headline": {"type": "string", "description": "Report title, at most 12 words, no rating or numbers."},
        "summary": {"type": "string", "description": "2-3 sentences: the rating, the price target and the core view."},
        "thesis": {"type": "array", "description": "Exactly three pillars of the view.",
                   "items": {"type": "object", "additionalProperties": False, "required": ["title", "text"],
                             "properties": {"title": {"type": "string"}, "text": {"type": "string"}}}},
        "business_overview": {"type": "string", "description": "What the company sells, to whom, how it makes money."},
        "valuation": {"type": "string", "description": "How the target is built, what the scenarios and sensitivity say."},
        "risks": {"type": "array", "description": "3-5 risks to the rating (for Underweight include upside risks).",
                  "items": {"type": "object", "additionalProperties": False, "required": ["title", "text"],
                            "properties": {"title": {"type": "string"}, "text": {"type": "string"}}}},
        "what_would_change": {"type": "array", "description": "3 observable developments that would change the view.",
                              "items": {"type": "string"}},
    },
}

SYSTEM = """You are a senior sell-side equity research analyst covering US software. You write the \
narrative of an initiation report for institutional investors, in the house style of a bulge-bracket bank: \
concise, specific, measured, third person plural ("we"), no hype, no exclamation marks.

The rating, price target and scenarios were set by a quantitative model and are given to you as facts. \
Explain and support them; never change, soften or contradict them. If the model flags the target as an \
outlier, say so plainly in the valuation section and explain what drives it. Be candid about the model's \
limitations where they matter (for example a DCF that is harsh on fast growers, or a noisy peer regression).

Numbers (strict):
- Use only numbers from the fact sheet, the 10-K excerpt or the risk summary. Never estimate, re-round \
differently, or bring in outside figures, news, guidance or events.
- After every number taken from the fact sheet, add its tag in square brackets, e.g. "a price target of \
$388 [F3]". Numbers quoted from the 10-K excerpt need no tag.
- Differences you compute between two fact-sheet percentages are allowed (e.g. "15 points above the median").

Content:
- Business overview: what the company sells, to whom and how it is paid, from the 10-K excerpt. No marketing language.
- Thesis: exactly three pillars, each a short title and 2-3 sentences tied to the numbers.
- Risks: risks to the rating. For Overweight, what could make it fall short; for Underweight, what could \
make it outperform. Use the risk summary where relevant.
- What would change our view: three observable developments (a metric crossing a level, a trend reversing).
- Keep the whole narrative under 900 words."""


# ---------------------------------------------------------------------------
# The fact sheet: every number the narrative may use
# ---------------------------------------------------------------------------

def fact_sheet(c: CompanyReport) -> list[tuple[str, float, str, str]]:
    """[(label, value, kind, shown)], kind in money/pct/multiple/plain."""
    t, d, row, f = c.target, c.dcf, c.comps_row, c.fiscal
    cy = f"calendar {c.calendar_year}"          # say which year, not "current year"
    rows: list[tuple[str, float, str]] = []

    def add(label, value, kind):
        if finite(value):
            rows.append((label, float(value), kind))

    add("Share price", t["price"], "money")
    add("12-month price target", t["price_target"], "money")
    add("Expected return to target", t["expected_return"], "pct")
    add("Bear-case target", t["bear_target"], "money")
    add("Bull-case target", t["bull_target"], "money")
    add("Bear-case return", t["bear_return"], "pct")
    add("Bull-case return", t["bull_return"], "pct")
    add("Fair value today (blend)", t["fair_value"], "money")
    add("DCF value per share", t["dcf_value"], "money")
    add("Comps-implied value per share", t["comps_value"], "money")
    add("Cost of equity", t["cost_of_equity"], "pct")
    add("WACC", d["wacc"], "pct")
    add("Terminal growth", c.dcf_cfg["terminal_growth_rate"], "pct")
    add("Share of DCF value from terminal value", d["tv_pct_ev"], "pct")
    add("Revenue growth the share price implies (reverse DCF)", d["market_implied_growth"], "pct")
    add("Starting revenue growth in the DCF", d["start_growth"], "pct")
    add("Mature FCF margin assumed", d["target_fcf_margin"], "pct")
    lo, hi = c.week52
    add("52-week low", lo, "money")
    add("52-week high", hi, "money")
    add("Market capitalization", row["market_cap"], "money")
    add("Enterprise value", row["enterprise_value"], "money")
    add(f"EV / revenue ({cy})", row["ev_revenue"], "multiple")
    add(f"EV / EBITDA ({cy})", row["ev_ebitda"], "multiple")
    add(f"P / FCF ({cy})", row["p_fcf"], "multiple")
    add(f"Revenue growth ({cy})", row["revenue_growth"], "pct")
    add(f"Rule of 40 ({cy})", row["rule_of_40"], "pct")
    add(f"Stock-based pay as % of revenue ({cy})", row["sbc_pct_revenue"], "pct")
    peers = c.peers.drop(index=c.ticker, errors="ignore")
    add(f"Peer median EV / revenue ({cy})", peers["ev_revenue"].median(), "multiple")
    add(f"Peer median revenue growth ({cy})", peers["revenue_growth"].median(), "pct")
    add(f"Peer median FCF margin ({cy})", peers["fcf_margin"].median(), "pct")
    add("Peer regression R² (EV/revenue on growth)", c.regression.get("r_squared"), "pct")
    add("Universe median expected return (rating reference)", c.reference_return, "pct")
    for year, r in f.iterrows():
        fy = f"FY{year}"
        add(f"Revenue {fy}", r["revenue"], "money")
        add(f"Revenue growth {fy}", r["revenue_growth"], "pct")
        add(f"Gross margin {fy}", r["gross_margin"], "pct")
        add(f"Operating margin {fy}", r["operating_margin"], "pct")
        add(f"FCF {fy}", r["fcf"], "money")
        add(f"FCF margin {fy}", r["fcf_margin"], "pct")
        add(f"Stock-based pay % of revenue {fy}", r["sbc_pct_revenue"], "pct")
    return [(label, v, kind, show(v, kind)) for label, v, kind in rows]


def show(v: float, kind: str) -> str:
    if kind == "money":
        if abs(v) >= 1e9:
            return f"${v / 1e9:,.1f}B"
        if abs(v) >= 1e6:
            return f"${v / 1e6:,.0f}M"
        if float(v).is_integer() or abs(v) >= 1000:
            return f"${v:,.0f}"              # price targets are whole dollars: "$388", not "$388.00"
        return f"${v:,.2f}"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "multiple":
        return f"{v:.1f}x"
    return f"{v:,.2f}"


def tagged(c: CompanyReport) -> dict[str, tuple[str, float, str, str]]:
    return {f"F{i}": row for i, row in enumerate(fact_sheet(c), 1)}


# ---------------------------------------------------------------------------
# Request and checking
# ---------------------------------------------------------------------------

def risk_digest(risk: dict | None) -> str:
    if not risk:
        return "(not available)"
    lines = [f"Summary: {risk['summary']}",
             f"AI disruption risk: {risk['ai_disruption']['level']}. {risk['ai_disruption']['assessment']}"]
    lines += [f"- {k['title']} ({k['severity']}): {k['explanation']}" for k in risk["top_risks"]]
    return "\n".join(lines)


def build_request(c: CompanyReport, business: str, cfg: dict) -> dict:
    r, t = cfg["research"], c.target
    facts = "\n".join(f"[{tag}] {label}: {shown}" for tag, (label, _, _, shown) in tagged(c).items())
    flag = ("YES: the target implies a move beyond the model's outlier threshold; say so and explain the driver."
            if bool(t["outlier"]) else "no")
    user = (f"Company: {c.name} ({c.ticker}). Valuation date: {c.valuation_date}.\n"
            f"Rating: {t['rating']} (relative to an 18-company US software coverage universe).\n"
            f"Price target basis: {t['basis']}. Outlier flag: {flag}\n\n"
            f"<fact_sheet>\n{facts}\n</fact_sheet>\n\n"
            f"<risk_summary source=\"10-K Item 1A, summarized\">\n{risk_digest(c.risk)}\n</risk_summary>\n\n"
            f"<business source=\"10-K Item 1\">\n{business}\n</business>\n\n"
            "Write the report narrative.")
    return {"model": r["model"], "system": SYSTEM, "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": r["effort"], "format": {"type": "json_schema", "schema": SCHEMA}}}


def all_text(result: dict) -> str:
    parts = [result["headline"], result["summary"], result["business_overview"], result["valuation"]]
    parts += [f"{p['title']} {p['text']}" for p in result["thesis"] + result["risks"]]
    return "\n".join(parts + list(result["what_would_change"]))


def check(result: dict, c: CompanyReport, business: str) -> dict:
    facts = {tag: (v, kind) for tag, (_, v, kind, _) in tagged(c).items()}
    g = grounding.check(all_text(result), facts, [business, risk_digest(c.risk)])
    problems = [f"numbers not in your sources: {', '.join(g.unverified)}"] if g.unverified else []
    problems += [f"fact tags that don't exist: {', '.join(g.bad_tags)}"] if g.bad_tags else []
    t = c.target
    if t["rating"] not in result["summary"]:
        problems.append(f"the summary must state the rating ({t['rating']})")
    if f"${t['price_target']:,.0f}" not in result["summary"]:
        problems.append(f"the summary must state the price target (${t['price_target']:,.0f})")
    if len(result["thesis"]) != 3:
        problems.append("the thesis must have exactly three pillars")
    return {"ok": not problems, "problems": problems}


def cost(usage: dict, r: dict) -> float:
    return (usage["input_tokens"] * r["price_input_per_mtok"] + usage["output_tokens"] * r["price_output_per_mtok"]) / 1e6


def write(client, c: CompanyReport, business: str, cfg: dict) -> dict:
    r = cfg["research"]
    request = build_request(c, business, cfg)
    usage = {"input_tokens": 0, "output_tokens": 0}
    meta = {"ticker": c.ticker, "valuation_date": str(c.valuation_date), "prompt_version": r["prompt_version"],
            "rating": c.target["rating"], "price_target": float(c.target["price_target"]),
            "model_requested": r["model"], "generated_at": datetime.now().isoformat(timespec="seconds")}
    for attempt in range(2):
        # Streaming avoids HTTP timeouts on long inputs; fallbacks="default" retries on Anthropic's
        # recommended model if this one declines (same setup as the risk layer).
        with client.beta.messages.stream(max_tokens=r["max_tokens"], betas=["server-side-fallback-2026-07-01"],
                                         fallbacks="default", **request) as stream:
            message = stream.get_final_message()
        usage["input_tokens"] += message.usage.input_tokens
        usage["output_tokens"] += message.usage.output_tokens
        meta.update(model_served=message.model, usage=usage, cost_usd=cost(usage, r), attempts=attempt + 1)
        if message.stop_reason in ("refusal", "max_tokens"):
            return {**meta, "status": "refused" if message.stop_reason == "refusal" else "truncated"}
        text = next(b.text for b in message.content if b.type == "text")
        result = json.loads(text)
        verdict = check(result, c, business)
        if verdict["ok"]:
            return {**meta, "status": "ok", **result}
        if attempt == 0:
            meta["first_attempt_problems"] = verdict["problems"]
            turn = [b.model_dump() if hasattr(b, "model_dump") else b for b in message.content]
            request["messages"] = request["messages"] + [
                {"role": "assistant", "content": turn},
                {"role": "user", "content": "The draft has problems: " + "; ".join(verdict["problems"])
                 + ". Rewrite the whole narrative fixing them, following every rule."}]
    return {**meta, "status": "needs_review", "problems": verdict["problems"], **result}


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

def cache_path(cfg: dict, ticker: str) -> Path:
    return project_path(cfg["data"]["processed_dir"]) / "research" / f"{ticker}.json"


def cached(cfg: dict, c: CompanyReport) -> dict | None:
    p = cache_path(cfg, c.ticker)
    if not p.exists():
        return None
    old = json.loads(p.read_text())
    same = (old.get("valuation_date") == str(c.valuation_date) and old.get("prompt_version") ==
            cfg["research"]["prompt_version"] and old.get("price_target") == float(c.target["price_target"]))
    return old if same and old.get("status") == "ok" else None


def business_for(cfg: dict, c: CompanyReport) -> str | None:
    if not c.risk:
        return None
    return business_section(project_path(cfg["data"]["raw_dir"]) / "10k", c.ticker, c.risk["accession"],
                            cfg["research"]["business_max_words"])


def run(tickers: list[str] | None, estimate_only: bool, assume_yes: bool, refresh: bool) -> None:
    import os
    load_dotenv(PROJECT_ROOT / ".env")
    cfg = load_config()
    r = cfg["research"]
    u = load_universe(cfg)
    todo = []
    for tk in tickers or list(u.comps.index):
        c = u.company(tk)
        if not refresh and cached(cfg, c):
            continue
        business = business_for(cfg, c)
        if business is None:
            print(f"{tk}: no 10-K text cached; run the risk layer (models.risk) first. Skipped.")
            continue
        todo.append((c, business))
    if not todo:
        print("Nothing to do: every narrative is up to date.")
        return

    client = None
    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
    total_in = 0
    for c, business in todo:
        req = build_request(c, business, cfg)
        if client:
            total_in += client.messages.count_tokens(model=req["model"], system=req["system"],
                                                     messages=req["messages"]).input_tokens
        else:
            total_in += int((len(req["system"]) + len(req["messages"][0]["content"])) / 3.5)
    est = cost({"input_tokens": total_in, "output_tokens": r["estimated_output_tokens"] * len(todo)}, r)
    print(f"{len(todo)} narrative(s) to write with {r['model']}: {total_in:,} input tokens "
          f"[{'exact' if client else 'approximate, no key'}], estimated ${est:.2f} "
          f"(~${est / len(todo):.2f} each; a draft that needs its one retry costs about double).")
    if estimate_only or client is None:
        return
    if not assume_yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Cancelled; nothing was billed.")
        return
    spent = 0.0
    for c, business in todo:
        res = write(client, c, business, cfg)
        cache_path(cfg, c.ticker).parent.mkdir(parents=True, exist_ok=True)
        cache_path(cfg, c.ticker).write_text(json.dumps(res, indent=2))
        spent += res.get("cost_usd", 0.0)
        extra = f" problems: {res['problems']}" if res["status"] == "needs_review" else ""
        print(f"{c.ticker}: {res['status']} in {res.get('attempts', 1)} attempt(s), ${res.get('cost_usd', 0):.2f}{extra}")
    print(f"Total spent: ${spent:.2f}")


def main() -> None:
    p = argparse.ArgumentParser(description="Write equity research narratives with Claude.")
    p.add_argument("--tickers", help="comma-separated, e.g. ADBE,HUBS")
    p.add_argument("--estimate", action="store_true", help="count tokens and estimate cost; nothing billed")
    p.add_argument("--yes", action="store_true", help="don't ask before spending")
    p.add_argument("--refresh", action="store_true", help="rewrite even if a current narrative is cached")
    a = p.parse_args()
    run(a.tickers.split(",") if a.tickers else None, a.estimate, a.yes, a.refresh)


if __name__ == "__main__":
    main()
