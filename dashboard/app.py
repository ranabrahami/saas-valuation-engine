"""SaaS Valuation Engine: interactive dashboard (Stage 6).

Run from the project root, after the pipeline and comps have been run once:
    streamlit run dashboard/app.py

Every number is recomputed from the sidebar assumptions on each change,
using the same model code as the command-line scripts. Nothing here is
saved back to config.yaml; "Reset" returns to its values.
"""

import copy

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from saas_valuation.config import load_config, project_path
from saas_valuation.models.dcf import adjusted_beta, value_universe
from saas_valuation.models.ranking import build_ranking

st.set_page_config(page_title="SaaS Valuation Engine", layout="wide")

BUCKETS = {"mature_profitable": "Mature, profitable", "growth_profitable": "Growth, profitable",
           "high_growth_unprofitable": "High growth, unprofitable", "maturing": "Maturing"}
VERDICT_COLORS = {"Undervalued": "#2e7d32", "In line": "#b8860b", "Overvalued": "#c62828"}
STAGE_COLORS = {"actual": "#7f7f7f", "explicit": "#1f3864", "fade": "#8eaadb"}
REGRESSORS = {"revenue_growth": "Revenue growth", "rule_of_40": "Rule of 40"}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@st.cache_data
def load_inputs() -> tuple[dict, dict[str, pd.DataFrame]]:
    cfg = load_config()
    processed = project_path(cfg["data"]["processed_dir"])
    tables = {name: pd.read_csv(processed / f"{name}.csv", index_col="ticker")
              for name in ("comps", "ev_bridge", "market_data")}
    for name in ("stock_splits", "overrides_log", "data_quality_report"):
        tables[name] = pd.read_csv(processed / f"{name}.csv")
    return cfg, tables


@st.cache_data(show_spinner="Revaluing 18 companies...")
def compute(cfg: dict, comps: pd.DataFrame, bridge: pd.DataFrame, market: pd.DataFrame):
    summary, results, valuation_date, _ = value_universe(comps, bridge, market, cfg)
    ranked = build_ranking(comps, bridge, market, summary, cfg)
    return summary, results, ranked["ranking"], ranked["regression"], valuation_date


try:
    base_cfg, data = load_inputs()
except FileNotFoundError as exc:
    st.error(f"Missing `{exc.filename}`. Run the pipeline and comps first:\n\n"
             "```\npython -m saas_valuation.data.pipeline\npython -m saas_valuation.models.comps\n```")
    st.stop()

comps, bridge, market = data["comps"], data["ev_bridge"], data["market_data"]


# ---------------------------------------------------------------------------
# Sidebar: assumptions
# ---------------------------------------------------------------------------

def pct_slider(label: str, key: str, default: float, low: float, high: float, step: float,
               help: str | None = None) -> float:
    """Slider shown in percent, returned as a decimal."""
    return st.sidebar.slider(label, low * 100, high * 100, default * 100, step * 100,
                             format="%.1f%%", key=key, help=help) / 100


st.sidebar.title("Assumptions")
if st.sidebar.button("Reset to config.yaml", width="stretch"):
    st.session_state.clear()
    st.rerun()

cfg = copy.deepcopy(base_cfg)
d, r = cfg["dcf"], cfg["ranking"]

st.sidebar.subheader("Cost of capital")
d["risk_free_rate"] = pct_slider("Risk-free rate", "rf", d["risk_free_rate"], 0.02, 0.06, 0.001,
                                 "10-year US Treasury yield. Higher rates lower every valuation.")
d["equity_risk_premium"] = pct_slider("Equity risk premium", "erp", d["equity_risk_premium"], 0.03, 0.08, 0.001,
                                      "Extra return investors demand for owning stocks over Treasuries.")
d["blume_adjust_beta"] = st.sidebar.checkbox("Blume-adjust betas", d["blume_adjust_beta"], key="blume",
                                             help="Pull raw betas toward 1.0: 2/3 x raw + 1/3.")

st.sidebar.subheader("Projection")
d["terminal_growth_rate"] = pct_slider("Terminal growth", "tg", d["terminal_growth_rate"], 0.01, 0.04, 0.0025,
                                       "Growth forever after year 10. Keep at or below long-run GDP growth.")
d["target_fcf_margin"] = pct_slider("Mature FCF margin", "tm", d["target_fcf_margin"], 0.10, 0.45, 0.01,
                                    "Margin every company converges to by year 10 (unless already above it).")
d["max_starting_growth"] = pct_slider("Cap on starting growth", "cap", d["max_starting_growth"], 0.10, 0.60, 0.05)
d["fade_years"] = st.sidebar.slider("Fade years", 0, 10, d["fade_years"], key="fade",
                                    help="Years after the 5 explicit ones in which growth glides to the terminal rate. "
                                         "0 = a plain 5-year model.")
d["sbc_treatment"] = st.sidebar.radio(
    "Stock-based comp", ["expense", "ignore"], index=["expense", "ignore"].index(d["sbc_treatment"]),
    key="sbc", format_func={"expense": "Expense (real cost)", "ignore": "Add back (non-cash)"}.get,
    horizontal=True)

st.sidebar.subheader("Ranking")
w = st.sidebar.slider("DCF weight in score", 0, 100, int(r["dcf_upside_weight"] * 100), 5, key="w",
                      format="%d%%", help="The rest goes to the comps signal.") / 100
r["dcf_upside_weight"], r["comps_weight"] = w, 1 - w
r["comps_regressor"] = st.sidebar.selectbox(
    "Comps benchmark", list(REGRESSORS), index=list(REGRESSORS).index(r["comps_regressor"]),
    format_func=REGRESSORS.get, key="reg")

# Per-company views live in session state (not widget keys) so they survive
# switching companies; callbacks update them before the rerun computes.
views: dict = st.session_state.setdefault("company_views", {})
d["company_overrides"] = {t: dict(v) for t, v in views.items()}
if views:
    st.sidebar.info(f"Your own view applied to: {', '.join(sorted(views))}")

summary, results, ranking, regression, valuation_date = compute(cfg, comps, bridge, market)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("SaaS Valuation Engine")
st.caption(f"18 US-listed SaaS companies · valued as of {valuation_date} · market data "
           f"{market['fetched_at'].max()[:10]} · financials from SEC 10-K filings · "
           "educational project, not investment advice")

tab_rank, tab_company, tab_comps, tab_data = st.tabs(["Ranking", "Company", "Comps", "Data & method"])


def pct(series: pd.Series) -> pd.Series:
    return series * 100


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

with tab_rank:
    counts = ranking["verdict"].value_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Undervalued", counts.get("Undervalued", 0), border=True)
    c2.metric("Overvalued", counts.get("Overvalued", 0), border=True)
    c3.metric("Median DCF upside", f"{summary['upside'].median():+.0%}", border=True)
    c4.metric(f"Comps fit (R²) on {REGRESSORS[regression['regressor']].lower()}",
              f"{regression['r_squared']:.2f}", border=True)

    table = pd.DataFrame({
        "Rank": ranking["rank"], "Company": ranking["name"], "Profile": ranking["bucket"].map(BUCKETS),
        "Verdict": ranking["verdict"], "Score": ranking["score"], "Price": ranking["price"],
        "DCF price": ranking["dcf_price"], "Comps price": ranking["comps_price"],
        "DCF upside": pct(ranking["dcf_upside"]), "Comps upside": pct(ranking["comps_upside"]),
        "Blended upside": pct(ranking["blended_upside"]),
        "Signals agree": ranking["signals_agree"],
    })
    money = st.column_config.NumberColumn(format="$%.2f")
    upside = st.column_config.NumberColumn(format="%+.0f%%")
    st.dataframe(table, width="stretch", height=670, column_config={
        "Score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f"),
        "Price": money, "DCF price": money, "Comps price": money,
        "DCF upside": upside, "Comps upside": upside, "Blended upside": upside,
    })

    st.subheader("Do the two methods agree?")
    st.caption("Top-right: cheap on both. Bottom-left: expensive on both. "
               "The other two quadrants are where the market and the cash flows disagree.")
    points = ranking.reset_index().rename(columns={"index": "ticker"})
    base = alt.Chart(points).encode(
        x=alt.X("dcf_upside:Q", title="DCF upside", axis=alt.Axis(format="%")),
        y=alt.Y("comps_upside:Q", title="Comps upside", axis=alt.Axis(format="%")),
        color=alt.Color("verdict:N", scale=alt.Scale(domain=list(VERDICT_COLORS),
                                                     range=list(VERDICT_COLORS.values())), title="Verdict"),
        tooltip=["ticker", "name", alt.Tooltip("dcf_upside:Q", format="+.0%"),
                 alt.Tooltip("comps_upside:Q", format="+.0%"), alt.Tooltip("score:Q", format=".0f")])
    zero = pd.DataFrame({"v": [0]})
    chart = (base.mark_circle(size=140, opacity=0.85)
             + base.mark_text(dx=9, align="left", fontSize=12).encode(text="ticker:N")
             + alt.Chart(zero).mark_rule(strokeDash=[4, 4], color="gray").encode(x="v:Q")
             + alt.Chart(zero).mark_rule(strokeDash=[4, 4], color="gray").encode(y="v:Q"))
    st.altair_chart(chart.properties(height=460), width="stretch")


# ---------------------------------------------------------------------------
# Company deep dive
# ---------------------------------------------------------------------------

def baseline_view(ticker: str) -> dict:
    """The model's own assumptions for a company, before any personal view."""
    margin_col = "fcf_margin_after_sbc" if d["sbc_treatment"] == "expense" else "fcf_margin"
    row = comps.loc[ticker]
    beta, _ = adjusted_beta(market.loc[ticker, "beta"], d["default_beta"], d["blume_adjust_beta"])
    return {"starting_growth": min(row["revenue_growth"], d["max_starting_growth"]),
            "target_fcf_margin": max(row[margin_col], d["target_fcf_margin"]), "beta": beta}


def sync_view(ticker: str, defaults: dict) -> None:
    if not st.session_state.get(f"on_{ticker}"):
        views.pop(ticker, None)
        return
    views[ticker] = {
        "starting_growth": st.session_state.get(f"g_{ticker}", defaults["starting_growth"] * 100) / 100,
        "target_fcf_margin": st.session_state.get(f"m_{ticker}", defaults["target_fcf_margin"] * 100) / 100,
        "beta": st.session_state.get(f"b_{ticker}", defaults["beta"]),
    }


with tab_company:
    order = list(ranking.index)
    ticker = st.selectbox("Company", order, format_func=lambda t: f"{t} · {comps.loc[t, 'name']}",
                          key="company")
    s, res, rk = summary.loc[ticker], results[ticker], ranking.loc[ticker]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Share price", f"${s['price']:,.2f}", border=True)
    c2.metric("DCF price", "NM" if pd.isna(s["implied_price"]) else f"${s['implied_price']:,.2f}",
              None if pd.isna(s["upside"]) else f"{s['upside']:+.0%}", border=True)
    c3.metric("Comps price", f"${rk['comps_price']:,.2f}", f"{rk['comps_upside']:+.0%}", border=True)
    c4.metric("Rank", f"{int(rk['rank'])} of {len(ranking)}", rk["verdict"], delta_color="off", border=True)
    c5.metric("Growth implied by price",
              ">150%" if pd.isna(s["market_implied_growth"]) else f"{s['market_implied_growth']:.0%}",
              f"actual {comps.loc[ticker, 'revenue_growth']:.0%}", delta_color="off", border=True,
              help="Reverse DCF: the starting growth that would make the DCF price equal today's price.")

    defaults = baseline_view(ticker)
    with st.expander("Use my own assumptions for this company", expanded=ticker in views):
        st.checkbox(f"Apply my view to {ticker}", key=f"on_{ticker}", value=ticker in views,
                    on_change=sync_view, args=(ticker, defaults))
        enabled = ticker in views
        current = views.get(ticker, defaults)
        o1, o2, o3 = st.columns(3)
        o1.slider("Starting revenue growth", -10.0, 60.0, float(current["starting_growth"] * 100), 0.5,
                  format="%.1f%%", key=f"g_{ticker}", disabled=not enabled,
                  on_change=sync_view, args=(ticker, defaults))
        o2.slider("Mature FCF margin", 0.0, 50.0, float(current["target_fcf_margin"] * 100), 0.5,
                  format="%.1f%%", key=f"m_{ticker}", disabled=not enabled,
                  on_change=sync_view, args=(ticker, defaults))
        o3.slider("Beta", 0.5, 2.5, float(current["beta"]), 0.05, key=f"b_{ticker}", disabled=not enabled,
                  on_change=sync_view, args=(ticker, defaults))
        st.caption(f"Model defaults: growth {defaults['starting_growth']:.1%}, mature margin "
                   f"{defaults['target_fcf_margin']:.1%}, beta {defaults['beta']:.2f}. "
                   "Your view also feeds the ranking.")

    proj = res["projection"]
    margin_col = "fcf_margin_after_sbc" if d["sbc_treatment"] == "expense" else "fcf_margin"
    chart_data = pd.concat([
        pd.DataFrame({"year": [res["base_year"]], "stage": ["actual"], "revenue": [res["base_revenue"]],
                      "fcf": [res["base_revenue"] * s["start_fcf_margin"]], "growth": [np.nan],
                      "fcf_margin": [s["start_fcf_margin"]]}),
        proj.reset_index()[["year", "stage", "revenue", "fcf", "growth", "fcf_margin"]],
    ])
    chart_data["revenue_bn"], chart_data["fcf_bn"] = chart_data["revenue"] / 1e9, chart_data["fcf"] / 1e9

    left, right = st.columns(2)
    with left:
        st.markdown("**Revenue (bars) and free cash flow (line), $B**")
        bars = alt.Chart(chart_data).mark_bar().encode(
            x=alt.X("year:O", title=None), y=alt.Y("revenue_bn:Q", title="$B"),
            color=alt.Color("stage:N", scale=alt.Scale(domain=list(STAGE_COLORS),
                                                       range=list(STAGE_COLORS.values())), title="Stage"),
            tooltip=["year", alt.Tooltip("revenue_bn:Q", format=".2f", title="Revenue $B"), "stage"])
        line = alt.Chart(chart_data).mark_line(point=True, color="#c55a11").encode(
            x="year:O", y="fcf_bn:Q", tooltip=["year", alt.Tooltip("fcf_bn:Q", format=".2f", title="FCF $B")])
        st.altair_chart((bars + line).properties(height=320), width="stretch")
    with right:
        st.markdown("**Revenue growth and FCF margin**")
        long = chart_data.melt(id_vars="year", value_vars=["growth", "fcf_margin"], var_name="metric")
        long["metric"] = long["metric"].map({"growth": "Revenue growth", "fcf_margin": "FCF margin"})
        st.altair_chart(alt.Chart(long.dropna()).mark_line(point=True).encode(
            x=alt.X("year:O", title=None), y=alt.Y("value:Q", title=None, axis=alt.Axis(format="%")),
            color=alt.Color("metric:N", title=None),
            tooltip=["year", "metric", alt.Tooltip("value:Q", format=".1%")]).properties(height=320),
            width="stretch")

    left, right = st.columns([2, 3])
    with left:
        st.markdown("**Valuation bridge**")
        bridge_rows = [
            ("Cost of equity (CAPM)", f"{s['cost_of_equity']:.1%}  (beta {s['beta']:.2f})"),
            ("WACC", f"{s['wacc']:.1%}"),
            ("PV of free cash flow", f"${s['sum_pv_fcf'] / 1e9:,.1f}B"),
            ("PV of terminal value", f"${s['pv_terminal_value'] / 1e9:,.1f}B  ({s['tv_pct_ev']:.0%} of EV)"),
            ("Enterprise value", f"${s['enterprise_value'] / 1e9:,.1f}B"),
            ("+ Net cash", f"${s['net_cash'] / 1e9:,.1f}B"),
            ("x Roll-forward to today", f"{s['roll_forward_factor']:.3f}"),
            ("= Equity value", f"${s['equity_value'] / 1e9:,.1f}B"),
            ("÷ Shares", f"{s['shares'] / 1e6:,.0f}M"),
            ("= DCF price", "NM" if pd.isna(s["implied_price"]) else f"${s['implied_price']:,.2f}"),
        ]
        st.dataframe(pd.DataFrame(bridge_rows, columns=["Step", "Value"]), hide_index=True, width="stretch")
    with right:
        st.markdown("**Sensitivity: DCF price by WACC (rows) and terminal growth (columns)**")
        grid = res["sensitivity"].copy()
        grid.index = [f"{v:.1%}" for v in grid.index]
        grid.columns = [f"{v:.1%}" for v in grid.columns]
        price = s["price"]
        styled = grid.style.format(lambda v: "NM" if pd.isna(v) else f"${v:,.0f}").map(
            lambda v: "" if pd.isna(v) else
            f"background-color: {'#d9ead3' if v > price else '#f4cccc'}; color: black")
        st.dataframe(styled, width="stretch")
        st.caption(f"Green = above today's ${price:,.2f}; red = below. Middle row is the base WACC.")

    if isinstance(s["notes"], str) and s["notes"]:
        st.info(s["notes"])


# ---------------------------------------------------------------------------
# Comps
# ---------------------------------------------------------------------------

with tab_comps:
    label = REGRESSORS[regression["regressor"]]
    st.subheader(f"EV / Revenue vs {label.lower()}")
    st.caption(f"ln(EV/Revenue) = {regression['intercept']:.2f} + {regression['slope']:.2f} x {label.lower()} · "
               f"R² = {regression['r_squared']:.2f} · each +10 points of {label.lower()} goes with a "
               f"{regression['multiple_change_per_10pts']:+.0%} higher multiple. Companies below the line are "
               "cheap for their fundamentals.")
    pts = ranking.reset_index().rename(columns={"index": "ticker"}).dropna(subset=["regressor", "ev_revenue"])
    xs = np.linspace(pts["regressor"].min(), pts["regressor"].max(), 60)
    fit = pd.DataFrame({"regressor": xs, "fitted": np.exp(regression["intercept"] + regression["slope"] * xs)})
    scatter = alt.Chart(pts).encode(
        x=alt.X("regressor:Q", title=label, axis=alt.Axis(format="%")),
        y=alt.Y("ev_revenue:Q", title="EV / Revenue (log scale)", scale=alt.Scale(type="log")),
        color=alt.Color("bucket:N", title="Profile", legend=alt.Legend(labelExpr=
            " || ".join(f"datum.value == '{k}' ? '{v}'" for k, v in BUCKETS.items()) + " : datum.value")),
        tooltip=["ticker", "name", alt.Tooltip("regressor:Q", format=".0%", title=label),
                 alt.Tooltip("ev_revenue:Q", format=".1f", title="EV/Revenue"),
                 alt.Tooltip("fair_ev_revenue:Q", format=".1f", title="Fair EV/Revenue")])
    line = alt.Chart(fit).mark_line(color="gray", strokeDash=[6, 4]).encode(x="regressor:Q", y="fitted:Q")
    st.altair_chart((scatter.mark_circle(size=140) + scatter.mark_text(dx=9, align="left").encode(text="ticker:N")
                     + line).properties(height=480), width="stretch")

    st.subheader("Comparable companies (CY metrics)")
    ctable = pd.DataFrame({
        "Company": comps["name"], "EV / Revenue": comps["ev_revenue"], "EV / EBITDA": comps["ev_ebitda"],
        "P / FCF": comps["p_fcf"], "Revenue growth": pct(comps["revenue_growth"]),
        "Gross margin": pct(comps["gross_margin"]), "FCF margin": pct(comps["fcf_margin"]),
        "SBC % revenue": pct(comps["sbc_pct_revenue"]), "Rule of 40": pct(comps["rule_of_40"]),
    }).sort_values("EV / Revenue", ascending=False)
    mult = st.column_config.NumberColumn(format="%.1fx")
    pcol = st.column_config.NumberColumn(format="%.0f%%")
    st.dataframe(ctable, width="stretch", height=670, column_config={
        "EV / Revenue": mult, "EV / EBITDA": mult, "P / FCF": mult, "Revenue growth": pcol,
        "Gross margin": pcol, "FCF margin": pcol, "SBC % revenue": pcol, "Rule of 40": pcol})
    st.caption("Blank multiples are NM (negative earnings or above 100x). Full detail in outputs/comps_CY2025.xlsx.")


# ---------------------------------------------------------------------------
# Data & method
# ---------------------------------------------------------------------------

with tab_data:
    st.subheader("Assumptions changed from config.yaml")
    changes = [(f"{section}.{key}", base_cfg[section].get(key), cfg[section][key])
               for section in ("dcf", "ranking") for key in cfg[section]
               if key != "company_overrides" and cfg[section][key] != base_cfg[section].get(key)]
    changes += [(f"view: {t}", "model default", ", ".join(f"{k} {v:.2f}" for k, v in vals.items()))
                for t, vals in views.items()]
    if changes:
        st.dataframe(pd.DataFrame(changes, columns=["Assumption", "config.yaml", "Now"]).astype(str),
                     hide_index=True, width="stretch")
    else:
        st.write("None: everything matches config.yaml.")

    left, right = st.columns(2)
    with left:
        st.subheader("Stock splits in the data window")
        splits = data["stock_splits"]
        st.dataframe(splits[splits["in_data_window"]][["ticker", "date", "ratio", "source",
                                                       "share_values_adjusted"]],
                     hide_index=True, width="stretch")
        st.subheader("Values entered from 10-K filings")
        st.dataframe(data["overrides_log"][["ticker", "fiscal_year", "concept", "value_usd_millions",
                                            "status", "source"]], hide_index=True, width="stretch")
    with right:
        st.subheader("Data quality")
        quality = data["data_quality_report"]
        st.write(quality["severity"].value_counts().rename("issues").to_frame().T)
        st.dataframe(quality[quality["severity"].isin(["error", "warning"])]
                     [["ticker", "severity", "concept", "fiscal_year", "detail"]],
                     hide_index=True, width="stretch")

    st.subheader("Method in brief")
    st.markdown(f"""
- **DCF:** {cfg['dcf']['projection_years']} explicit + {d['fade_years']} fade years; WACC from CAPM; mid-year
  discounting; Gordon growth terminal value; stock-based comp {'expensed' if d['sbc_treatment'] == 'expense' else 'added back'}.
- **Comps:** ln(EV/Revenue) regressed on {REGRESSORS[r['comps_regressor']].lower()}; each company's fair multiple
  comes from the other companies (leave-one-out).
- **Score:** {w:.0%} DCF percentile + {1 - w:.0%} comps percentile. Verdicts are relative to these 18 companies.
- Full workings: `outputs/comps_CY2025.xlsx`, `outputs/dcf_models.xlsx`, `outputs/ranking.xlsx`.
""")
