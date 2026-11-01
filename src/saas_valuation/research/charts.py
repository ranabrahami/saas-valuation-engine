"""Print-quality charts for equity research reports.

Each function takes a CompanyReport and returns a matplotlib Figure; save()
writes SVG (crisp in the PDF, text kept as text) and a PNG preview.

House style, as in sell-side reports: one y-axis per chart (growth is written
on the revenue bars, never on a second axis), at most three series, direct
labels instead of a number on every point, recessive grid, the company in navy
and peers in grey, the price target in orange.

Run: python -m saas_valuation.research.charts HUBS ADBE     (default: all companies)
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import logging  # noqa: E402

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter, PercentFormatter  # noqa: E402

from saas_valuation.config import project_path  # noqa: E402
from saas_valuation.research.data import CompanyReport, finite, load_universe  # noqa: E402

NAVY = "#1f3864"         # the company / actuals
ESTIMATE = "#8eaadb"     # projections (lighter, same hue)
BLUE = "#2a78d6"         # second series
ORANGE = "#eb6834"       # price target, third series
AQUA = "#1baf7a"
GREY = "#b4b2a9"         # peers, ranges
INK, INK2, MUTED, GRID = "#1d1d1b", "#52514e", "#898781", "#e6e4dc"
BEAR, BULL = "#d9675f", "#5b9bd5"

WIDE, HALF = (7.0, 2.7), (3.45, 2.55)

plt.rcParams.update({
    "font.family": ["Inter", "Helvetica Neue", "Arial", "DejaVu Sans"],
    "font.size": 8, "axes.titlesize": 9, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "axes.titlecolor": INK, "axes.labelcolor": INK2, "axes.edgecolor": GREY, "axes.linewidth": 0.6,
    "xtick.color": INK2, "ytick.color": INK2, "xtick.labelsize": 7.5, "ytick.labelsize": 7.5,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False, "legend.fontsize": 7.5,
    "svg.fonttype": "none",                # keep text as text, so the PDF uses the page font
    "figure.dpi": 200, "savefig.bbox": "tight", "savefig.pad_inches": 0.04,
})


def billions(v, _=None) -> str:
    b = v / 1e9
    return f"${b:,.0f}B" if abs(b) >= 10 or float(b).is_integer() else f"${b:,.1f}B"


def short_fy(year) -> str:
    return f"FY{int(year) % 100:02d}"


def money(v: float) -> str:
    return f"${v:,.0f}"


# Shown as a caption under each chart in the report, as sell-side reports do.
SOURCES = {
    "price": "Yahoo Finance (daily close, split-adjusted); price target and scenarios from this model.",
    "revenue": "10-K filings via SEC EDGAR (fiscal-year actuals); model estimates (E, calendar years).",
    "margins": "10-K filings via SEC EDGAR. FCF = operating cash flow - capex - capitalized software.",
    "fcf": "10-K filings via SEC EDGAR. A missing bar means capex is not in the SEC's standardized data.",
    "football_field": "This model; Yahoo Finance for the 52-week trading range.",
    "comps": "This model, calendar-year comps for the 18-company coverage universe.",
}


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def price_chart(c: CompanyReport):
    """Two years of daily closes, then the 12-month target with the bear-bull band."""
    t, p = c.target, c.prices["close"]
    fig, ax = plt.subplots(figsize=WIDE)
    ax.plot(p.index, p.values, color=NAVY, lw=1.2)
    start = p.index.max()
    end = start + pd.DateOffset(months=12)
    ax.fill_between([start, end], t["bear_target"], t["bull_target"], color=ESTIMATE, alpha=0.25, lw=0)
    ax.plot([start, end], [t["price_target"]] * 2, color=ORANGE, lw=1.6, ls="--")
    ax.plot([start], [p.iloc[-1]], "o", color=NAVY, ms=4)
    ax.annotate(f"Price target {money(t['price_target'])}", (end, t["price_target"]), xytext=(4, 0),
                textcoords="offset points", va="center", fontsize=7.5, color=ORANGE, fontweight="bold")
    for label, v in (("Bull", t["bull_target"]), ("Bear", t["bear_target"])):
        ax.annotate(f"{label} {money(v)}", (end, v), xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=7, color=INK2)
    ax.annotate(f"{money(p.iloc[-1])}", (start, p.iloc[-1]), xytext=(6, -12), textcoords="offset points",
                ha="left", fontsize=7.5, color=NAVY, fontweight="bold")
    ax.set_xlim(p.index.min(), end + pd.DateOffset(months=3))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.set_title(f"{c.ticker} share price and 12-month price target")
    return fig


def revenue_chart(c: CompanyReport, estimate_years: int = 3):
    """Revenue bars: fiscal-year actuals, then calendar-year model estimates; growth written on top."""
    f = c.fiscal
    proj = c.projection.reset_index().head(estimate_years)
    labels = [short_fy(y) for y in f.index] + [f"{int(y) % 100:02d}E" for y in proj["year"]]
    values = list(f["revenue"]) + list(proj["revenue"])
    growth = list(f["revenue_growth"]) + list(proj["growth"])
    colors = [NAVY] * len(f) + [ESTIMATE] * len(proj)
    fig, ax = plt.subplots(figsize=HALF)
    bars = ax.bar(labels, values, color=colors, width=0.62)
    for b, g in zip(bars, growth):
        if finite(g):
            ax.annotate(f"{g:+.0%}", (b.get_x() + b.get_width() / 2, b.get_height()), xytext=(0, 2),
                        textcoords="offset points", ha="center", fontsize=6.5, color=INK2)
    ax.yaxis.set_major_formatter(FuncFormatter(billions))
    ax.set_title("Revenue and growth")
    return fig


def margin_chart(c: CompanyReport):
    f = c.fiscal
    fig, ax = plt.subplots(figsize=HALF)
    series = [("Gross margin", "gross_margin", NAVY), ("Operating margin", "operating_margin", BLUE),
              ("FCF margin", "fcf_margin", ORANGE)]
    x = [short_fy(y) for y in f.index]
    for label, col, color in series:
        ax.plot(x, f[col], color=color, lw=1.5, marker="o", ms=2.5, label=label)
        last = f[col].dropna()
        if len(last):
            ax.annotate(f"{last.iloc[-1]:.0%}", (len(x) - 1, last.iloc[-1]), xytext=(5, 0),
                        textcoords="offset points", va="center", fontsize=7, color=color)
    if (f[[s[1] for s in series]] < 0).any().any():
        ax.axhline(0, color=MUTED, lw=0.6)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.13), ncol=3, handlelength=1.2, columnspacing=0.8)
    ax.set_title("Margins")
    return fig


def fcf_chart(c: CompanyReport):
    """Free cash flow before and after stock-based pay: how much of the cash is paid out in shares."""
    f = c.fiscal
    x = np.arange(len(f))
    fig, ax = plt.subplots(figsize=HALF)
    ax.bar(x - 0.19, f["fcf"], width=0.36, color=NAVY, label="Free cash flow")
    ax.bar(x + 0.19, f["fcf_after_sbc"], width=0.36, color=ORANGE, label="FCF after stock-based pay")
    ax.axhline(0, color=MUTED, lw=0.6)
    for i, v in zip(x, f["fcf"]):
        if not finite(v):                  # missing, not zero: say so
            ax.annotate("n/a", (i, 0), xytext=(0, 3), textcoords="offset points", ha="center",
                        fontsize=7, color=MUTED)
    ax.set_xticks(x, [short_fy(y) for y in f.index])
    ax.yaxis.set_major_formatter(FuncFormatter(billions))
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.08), ncol=2, handlelength=1.2)
    ax.set_title("Cash generation")
    return fig


def football_field(c: CompanyReport):
    """Valuation ranges side by side, with today's price and the target as vertical lines."""
    t = c.target
    grid = c.sensitivity.to_numpy(dtype=float)
    grid = grid[np.isfinite(grid)]
    rows = [("52-week trading range", *c.week52),
            ("DCF: WACC ±2pts, terminal growth 2-4%", grid.min() if grid.size else np.nan,
             grid.max() if grid.size else np.nan),
            ("Comps: fair multiple ±1 s.d.", t["bear_comps"], t["bull_comps"]),
            ("12-month target: bear to bull", t["bear_target"], t["bull_target"])]
    rows = [r for r in rows if finite(r[1]) and finite(r[2])]
    fig, ax = plt.subplots(figsize=(WIDE[0], 0.42 * len(rows) + 0.9))
    for i, (label, lo, hi) in enumerate(reversed(rows)):
        ax.barh(i, hi - lo, left=lo, height=0.5, color=GREY if i == len(rows) - 1 else ESTIMATE)
        ax.annotate(money(lo), (lo, i), xytext=(-4, 0), textcoords="offset points", ha="right", va="center",
                    fontsize=7, color=INK2)
        ax.annotate(money(hi), (hi, i), xytext=(4, 0), textcoords="offset points", ha="left", va="center",
                    fontsize=7, color=INK2)
    ax.set_yticks(range(len(rows)), [r[0] for r in reversed(rows)])
    ax.axvline(t["price"], color=NAVY, lw=1.2)
    ax.axvline(t["price_target"], color=ORANGE, lw=1.4, ls="--")
    top = len(rows) - 0.45
    ax.annotate(f"Price {money(t['price'])}", (t["price"], top), ha="center", va="bottom", fontsize=7, color=NAVY,
                fontweight="bold")
    ax.annotate(f"Target {money(t['price_target'])}", (t["price_target"], top + 0.32), ha="center", va="bottom",
                fontsize=7, color=ORANGE, fontweight="bold")
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.grid(axis="y", visible=False)
    lo = min(min(r[1] for r in rows), t["price"], t["price_target"])
    hi = max(max(r[2] for r in rows), t["price"], t["price_target"])
    pad = (hi - lo) * 0.12
    ax.set_xlim(max(lo - pad, 0), hi + pad)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.set_ylim(-0.6, len(rows) + 0.2)
    ax.set_title("Valuation summary (value per share)")
    return fig


def comps_chart(c: CompanyReport):
    """EV/revenue against growth for the coverage universe, with the fitted line."""
    peers, reg = c.peers.dropna(subset=["revenue_growth", "ev_revenue"]), c.regression
    fig, ax = plt.subplots(figsize=HALF)
    others = peers.drop(index=c.ticker, errors="ignore")
    ax.scatter(others["revenue_growth"], others["ev_revenue"], s=16, color=GREY, zorder=2)
    for tk, r in others.iterrows():
        ax.annotate(tk, (r["revenue_growth"], r["ev_revenue"]), xytext=(3, 1), textcoords="offset points",
                    fontsize=5.5, color=MUTED)
    xs = np.linspace(peers["revenue_growth"].min(), peers["revenue_growth"].max(), 50)
    ax.plot(xs, np.exp(reg["intercept"] + reg["slope"] * xs), color=INK2, lw=1, ls="--", zorder=1)
    me = peers.loc[c.ticker]
    ax.scatter([me["revenue_growth"]], [me["ev_revenue"]], s=42, color=NAVY, zorder=3)
    ax.annotate(c.ticker, (me["revenue_growth"], me["ev_revenue"]), xytext=(4, -9), textcoords="offset points",
                fontsize=7.5, color=NAVY, fontweight="bold")
    ax.set_yscale("log")
    lo, hi = peers["ev_revenue"].min(), peers["ev_revenue"].max()
    ticks = [v for v in (1, 2, 3, 5, 10, 20, 30, 50, 100) if lo / 1.3 <= v <= hi * 1.3]
    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.grid(axis="x", color=GRID, lw=0.6)
    ax.set_xlabel("Revenue growth (CY)")
    ax.set_ylabel("EV / revenue (log scale)")
    ax.set_title(f"Peers: price vs growth (R² {reg['r_squared']:.2f})")
    return fig


CHARTS = {"price": price_chart, "revenue": revenue_chart, "margins": margin_chart, "fcf": fcf_chart,
          "football_field": football_field, "comps": comps_chart}


def save(fig, path_stem: Path) -> None:
    path_stem.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path_stem.with_suffix(".svg"))
    fig.savefig(path_stem.with_suffix(".png"))
    plt.close(fig)


def draw_all(c: CompanyReport, out_dir: Path) -> list[Path]:
    paths = []
    for name, fn in CHARTS.items():
        save(fn(c), out_dir / name)
        paths.append(out_dir / f"{name}.svg")
    return paths


def run(tickers: list[str] | None = None) -> None:
    u = load_universe()
    tickers = tickers or list(u.comps.index)
    base = project_path(u.cfg["comps"]["output_dir"]) / "reports"
    for tk in tickers:
        draw_all(u.company(tk), base / tk / "charts")
        print(f"{tk}: {len(CHARTS)} charts -> {(base / tk / 'charts').relative_to(base.parent.parent)}")


if __name__ == "__main__":
    run(sys.argv[1:] or None)
