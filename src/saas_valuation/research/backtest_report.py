"""The backtest note: charts and a two-page PDF in the research house style (Report stage 5).

Every number in the text is filled in from the backtest results, so the note
can't drift from the data.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from saas_valuation.config import project_path
from saas_valuation.research import charts as ch
from saas_valuation.research.backtest import SIGNALS
from saas_valuation.research.render import TEMPLATES, svg_inline

plt = ch.plt


def ic_chart(rob: dict):
    ic = rob["ic_by_date"]
    fig, ax = plt.subplots(figsize=ch.HALF)
    labels = [d[2:7].replace("-", "/") for d in ic.index]
    colors = [ch.NAVY if v >= 0 else ch.BEAR for v in ic]
    ax.bar(labels, ic.to_numpy(), color=colors, width=0.62)
    ax.axhline(ic.mean(), color=ch.ORANGE, lw=1.3, ls="--")
    ax.annotate(f"mean {ic.mean():.2f}", (len(ic) - 0.5, ic.mean()), xytext=(0, 4), textcoords="offset points",
                ha="right", fontsize=7, color=ch.ORANGE, fontweight="bold")
    ax.axhline(0, color=ch.MUTED, lw=0.6)
    ax.tick_params(axis="x", rotation=60, labelsize=6.5)
    ax.set_title("Engine score: 12-month rank IC by start date")
    return fig


def spread_chart(per_date: pd.DataFrame):
    fig, ax = plt.subplots(figsize=ch.HALF)
    labels = [d[2:7].replace("-", "/") for d in per_date["date"]]
    v = per_date["spread_score"].to_numpy()
    ax.bar(labels, v, color=[ch.NAVY if x >= 0 else ch.BEAR for x in v], width=0.62)
    ax.axhline(0, color=ch.MUTED, lw=0.6)
    ax.yaxis.set_major_formatter(ch.PercentFormatter(1.0, decimals=0))
    ax.tick_params(axis="x", rotation=60, labelsize=6.5)
    ax.set_title("Cheapest third minus most expensive third, next 12 months")
    return fig


def signal_chart(summary: pd.DataFrame):
    s = summary["mean_ic"].iloc[::-1]
    fig, ax = plt.subplots(figsize=(ch.WIDE[0], 2.0))
    colors = [ch.GREY if lbl.startswith("Baseline") else ch.NAVY for lbl in s.index]
    ax.barh(s.index, s.to_numpy(), color=colors, height=0.55)
    for i, (lbl, v) in enumerate(s.items()):
        t = summary.at[lbl, "ic_t_nw"]
        ax.annotate(f"{v:+.2f}  (t {t:.1f})", (v, i), xytext=(4 if v >= 0 else -4, 0), textcoords="offset points",
                    ha="left" if v >= 0 else "right", va="center", fontsize=7, color=ch.INK2)
    ax.axvline(0, color=ch.MUTED, lw=0.6)
    ax.grid(axis="x", color=ch.GRID, lw=0.6)
    ax.grid(axis="y", visible=False)
    lo, hi = min(s.min(), 0), max(s.max(), 0)
    ax.set_xlim(lo - 0.12, hi + 0.14)
    ax.set_title("Mean 12-month rank IC by signal (Newey-West t-statistic)")
    return fig


def write_note(cfg: dict, res: dict, rob: dict, alt_summary: pd.DataFrame, alt_reg: str) -> Path:
    out = project_path(cfg["comps"]["output_dir"]) / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    per12, sum12 = res[12]
    _, sum6 = res[6]
    for name, fig in (("ic", ic_chart(rob)), ("spread", spread_chart(per12)), ("signals", signal_chart(sum12))):
        ch.save(fig, out / name)
    panel = res["panel"]
    eng = sum12.loc[SIGNALS["score"]]
    alt = alt_summary.loc[SIGNALS["score"]]
    cheap = sum12.loc[SIGNALS["cheap_ev_revenue"]]
    corr_cheap = panel.groupby("date").apply(lambda g: g["score"].rank().corr(g["cheap_ev_revenue"].rank()),
                                             include_groups=False).mean()
    dates12 = per12["date"]
    ctx = {
        "css": Markup((TEMPLATES / "report.css").read_text()),
        "svg": {k: Markup(svg_inline(out / f"{k}.svg")) for k in ("ic", "spread", "signals")},
        "today": date.today().strftime("%-d %B %Y"), "first": panel["date"].min(), "last": panel["date"].max(),
        "first12": dates12.min(), "last12": dates12.max(), "n_dates": int(eng["dates"]),
        "n_companies": panel["ticker"].nunique(), "eng": eng, "alt": alt, "alt_reg": {"rule_of_40": "Rule of 40", "revenue_growth": "revenue growth"}.get(alt_reg, alt_reg),
        "cheap": cheap, "corr_cheap": corr_cheap, "sum12": sum12, "sum6": sum6,
        "annual": rob["annual"], "loo_min": rob["leave_one_out"].min(), "loo_max": rob["leave_one_out"].max(),
        "loo_low": rob["leave_one_out"].idxmin(), "perm_p": rob["permutation_p"], "null95": rob["null_95"],
        "signals": SIGNALS,
    }
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]),
                      undefined=StrictUndefined)
    html = env.get_template("backtest.html.j2").render(**ctx)
    (out / "backtest_note.html").write_text(html)
    pdf = out / "backtest_note.pdf"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(pdf), format="Letter", print_background=True, prefer_css_page_size=True,
                 display_header_footer=True,
                 header_template='<div style="font: 7pt Helvetica; color: #7d7b74; width: 100%; padding: 0 0.6in;">'
                                 'Backtest note · engine valuation signal</div>',
                 footer_template='<div style="font: 7pt Helvetica; color: #7d7b74; width: 100%; padding: 0 0.6in; '
                                 'text-align: right;">Page <span class="pageNumber"></span> of '
                                 '<span class="totalPages"></span></div>',
                 margin={"top": "0.75in", "bottom": "0.7in", "left": "0.6in", "right": "0.6in"})
        browser.close()
    return pdf


def pct(v: float) -> str:
    return f"{v * 100:+.0f}%" if np.isfinite(v) else "n/a"
