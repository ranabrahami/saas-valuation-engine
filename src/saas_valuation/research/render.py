"""Assemble a company's report as HTML and print it to PDF with Chrome (Report stage 4).

Everything on the page comes from the models (targets, DCF, comps), the charts
(research/charts.py) and the checked narrative (research/narrative.py). A
company is only rendered if its narrative passed the number check.

Run:
    python -m saas_valuation.research.render ADBE HUBS      # default: every company with a narrative
    python -m saas_valuation.research.render --html ADBE    # also keep the HTML (for layout work)
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

import numpy as np
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from saas_valuation.config import project_path
from saas_valuation.research.charts import CHARTS, SOURCES, draw_all
from saas_valuation.research.data import CompanyReport, finite, load_universe

TEMPLATES = Path(__file__).parent / "templates"
PROFILES = {"mature_profitable": "Mature, profitable", "growth_profitable": "Growth, profitable",
            "high_growth_unprofitable": "High growth", "maturing": "Maturing"}


class Fmt:
    """Number formatting for the page; 'n/a' for anything missing."""

    @staticmethod
    def _ok(v) -> bool:
        return finite(v)

    def money(self, v) -> str:
        return f"${v:,.0f}" if self._ok(v) else "n/a"

    def price(self, v) -> str:
        return f"${v:,.2f}" if self._ok(v) else "n/a"

    def big(self, v) -> str:
        if not self._ok(v):
            return "n/a"
        return f"${v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"${v / 1e6:,.0f}M"

    def millions(self, v) -> str:
        if not self._ok(v):
            return "n/a"
        return f"({abs(v) / 1e6:,.0f})" if v < 0 else f"{v / 1e6:,.0f}"

    def pct(self, v, signed: bool = False, decimals: int = 0) -> str:
        if not self._ok(v):
            return "n/a"
        return f"{v * 100:+.{decimals}f}%" if signed else f"{v * 100:.{decimals}f}%"

    def mult(self, v) -> str:
        return f"{v:.1f}x" if self._ok(v) else "NM"


def strip_tags(text: str) -> str:
    """Fact tags like [F12] are for checking, not for readers."""
    return re.sub(r"\s*\[F\d+\]", "", text)


def clean_narrative(n: dict) -> dict:
    out = {k: strip_tags(v) if isinstance(v, str) else v for k, v in n.items()}
    out["thesis"] = [{"title": strip_tags(p["title"]), "text": strip_tags(p["text"])} for p in n["thesis"]]
    out["risks"] = [{"title": strip_tags(p["title"]), "text": strip_tags(p["text"])} for p in n["risks"]]
    out["what_would_change"] = [strip_tags(w) for w in n["what_would_change"]]
    return out


def svg_inline(path: Path) -> str:
    """The SVG markup without the XML prolog, to embed directly in the page."""
    text = path.read_text()
    return text[text.index("<svg"):]


def financial_table(c: CompanyReport, fmt: Fmt, estimate_years: int = 3) -> dict:
    f = c.fiscal
    proj = c.projection.reset_index().head(estimate_years)
    cols = [{"label": f"FY{int(y) % 100:02d}", "est": False} for y in f.index]
    cols += [{"label": f"{int(y) % 100:02d}E", "est": True} for y in proj["year"]]
    na = ["n/a"] * len(proj)

    def row(label, actual, estimate=None, sub=False):
        return {"label": label, "cells": actual + (estimate if estimate is not None else ["–"] * len(proj)), "sub": sub}

    rows = [
        row("Revenue", [fmt.millions(v) for v in f["revenue"]], [fmt.millions(v) for v in proj["revenue"]]),
        row("growth", [fmt.pct(v, signed=True) for v in f["revenue_growth"]],
            [fmt.pct(v, signed=True) for v in proj["growth"]], sub=True),
        row("Gross margin", [fmt.pct(v) for v in f["gross_margin"]]),
        row("Operating margin", [fmt.pct(v) for v in f["operating_margin"]]),
        row("Free cash flow", [fmt.millions(v) for v in f["fcf"]], na),
        row("FCF margin", [fmt.pct(v) for v in f["fcf_margin"]], na, sub=True),
        row("FCF after stock-based pay", [fmt.millions(v) for v in f["fcf_after_sbc"]],
            [fmt.millions(v) for v in proj["fcf"]]),
        row("margin", [fmt.pct(v / r) if finite(v) and r else "n/a" for v, r in zip(f["fcf_after_sbc"], f["revenue"])],
            [fmt.pct(v) for v in proj["fcf_margin"]], sub=True),
        row("Stock-based pay % revenue", [fmt.pct(v) for v in f["sbc_pct_revenue"]]),
    ]
    return {"columns": cols, "rows": rows}


def sensitivity_table(c: CompanyReport) -> dict:
    s, price = c.sensitivity, c.target["price"]
    base_rate = c.dcf["wacc"]
    base_growth = c.dcf_cfg["terminal_growth_rate"]
    rows = []
    for rate in s.index:
        cells = []
        for g in s.columns:
            v = s.at[rate, g]
            cls = "above" if finite(v) and v > price else "below" if finite(v) else ""
            if np.isclose(rate, base_rate) and np.isclose(float(g), base_growth):
                cls += " base"
            cells.append({"value": v, "cls": cls})
        rows.append({"rate": rate, "cells": cells})
    return {"growths": [float(g) for g in s.columns], "rows": rows}


def methodology(u, c: CompanyReport) -> dict:
    cfg = u.cfg
    pt, sc, rt, d = cfg["report"]["price_target"], cfg["report"]["scenarios"], cfg["report"]["rating"], cfg["dcf"]
    from saas_valuation.models.target import residual_sd
    sd = residual_sd(u.comps[cfg["ranking"]["comps_regressor"]], u.comps["ev_revenue"])
    labels = rt["labels"]
    if rt["basis"] == "relative":
        rating_text = (f"Ratings are relative to the coverage universe: {labels['up']} if the expected return beats the "
                       f"universe median ({c.reference_return * 100:+.0f}% at this date) by at least "
                       f"{rt['band'] * 100:.0f} points, {labels['down']} if it trails by at least {rt['band'] * 100:.0f} "
                       f"points, otherwise {labels['mid']}.")
    else:
        rating_text = (f"{labels['up']} if the expected return is at least {rt['band'] * 100:.0f}%, {labels['down']} "
                       f"if it is at most -{rt['band'] * 100:.0f}%, otherwise {labels['mid']}.")
    return {"dcf_weight": pt["dcf_weight"], "comps_weight": pt["comps_weight"], "horizon": pt["horizon_months"],
            "dividend_yield": pt["dividend_yield"], "explicit": d["projection_years"], "fade": d["fade_years"],
            "target_margin": d["target_fcf_margin"], "rf": d["risk_free_rate"], "erp": d["equity_risk_premium"],
            "n": len(u.comps), "r2": u.regression["r_squared"], "resid_sd": sd, "resid_factor": float(np.exp(sd)),
            "growth_shift": f"±{sc['bull']['starting_growth_delta'] * 100:.0f} points",
            "margin_shift": f"±{sc['bull']['target_fcf_margin_delta'] * 100:.0f} points",
            "rating_text": rating_text, "model": model_name(cfg["research"]["model"])}


def model_name(model_id: str) -> str:
    """'claude-opus-5-5' -> 'Claude Opus 5.5' for the methodology text."""
    parts = model_id.split("-")
    if len(parts) >= 3 and parts[0] == "claude":
        return f"Claude {parts[1].capitalize()} {'.'.join(parts[2:])}"
    return model_id


def build_html(u, c: CompanyReport, narrative: dict, chart_dir: Path) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      undefined=StrictUndefined)
    from markupsafe import Markup
    svg = {name: Markup(svg_inline(chart_dir / f"{name}.svg")) for name in CHARTS}
    fmt = Fmt()
    vd = c.valuation_date
    # The stylesheet goes in as Markup: escaping would turn the quotes around font names into &#34;
    # and the browser would silently drop every font rule.
    return env.get_template("report.html.j2").render(
        c=c, t=c.target, row=c.comps_row, dcf=c.dcf, n=clean_narrative(narrative), fmt=fmt, svg=svg,
        sources=SOURCES, css=Markup((TEMPLATES / "report.css").read_text()), profile=PROFILES.get(c.bucket, c.bucket),
        date_long=vd.strftime("%-d %B %Y"), price_date=vd.isoformat(), cy=str(c.calendar_year)[-2:],
        w52=c.week52, tg=c.dcf_cfg["terminal_growth_rate"],
        outlier_band=u.cfg["report"]["price_target"]["flag_return_beyond"],
        fin=financial_table(c, fmt), sens=sensitivity_table(c), m=methodology(u, c))


HEADER = ('<div style="font-family: Helvetica, Arial, sans-serif; font-size: 7pt; color: #7d7b74; width: 100%; '
          'padding: 0 0.6in; display: flex; justify-content: space-between;">'
          '<span>{name} ({ticker}) · Initiation of coverage</span><span>{date}</span></div>')
FOOTER = ('<div style="font-family: Helvetica, Arial, sans-serif; font-size: 7pt; color: #7d7b74; width: 100%; '
          'padding: 0 0.6in; display: flex; justify-content: space-between;">'
          '<span>Educational project · not investment advice · see disclosures</span>'
          '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>')


def print_pdf(html: str, pdf_path: Path, c: CompanyReport, browser) -> None:
    page = browser.new_page()
    page.set_content(html, wait_until="load")
    page.evaluate("document.fonts.ready")
    page.pdf(path=str(pdf_path), format="Letter", print_background=True, prefer_css_page_size=True,
             display_header_footer=True,
             header_template=HEADER.format(name=c.name, ticker=c.ticker, date=c.valuation_date.isoformat()),
             footer_template=FOOTER,
             margin={"top": "0.75in", "bottom": "0.7in", "left": "0.6in", "right": "0.6in"})
    page.close()


def narrative_for(u, ticker: str) -> dict | None:
    path = project_path(u.cfg["data"]["processed_dir"]) / "research" / f"{ticker}.json"
    if not path.exists():
        return None
    n = json.loads(path.read_text())
    return n if n.get("status") == "ok" else None


def run(tickers: list[str] | None = None, keep_html: bool = False) -> list[Path]:
    from playwright.sync_api import sync_playwright
    u = load_universe()
    base = project_path(u.cfg["comps"]["output_dir"]) / "reports"
    done = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        for tk in tickers or list(u.comps.index):
            n = narrative_for(u, tk)
            if n is None:
                print(f"{tk}: skipped, no checked narrative (run research.narrative first)")
                continue
            c = u.company(tk)
            if n.get("price_target") != float(c.target["price_target"]):
                print(f"{tk}: skipped, the narrative was written for a different target; rerun research.narrative")
                continue
            out = base / tk
            draw_all(c, out / "charts")
            html = build_html(u, c, n, out / "charts")
            if keep_html:
                (out / f"{tk}.html").write_text(html)
            pdf = out / f"{tk}_initiation_{c.valuation_date.isoformat()}.pdf"
            print_pdf(html, pdf, c, browser)
            done.append(pdf)
            print(f"{tk}: {pdf.relative_to(base.parent.parent)}")
        browser.close()
    return done


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Render equity research reports to PDF.")
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--html", action="store_true", help="also save the HTML")
    a = ap.parse_args()
    run(a.tickers or None, a.html)
