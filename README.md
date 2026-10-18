# SaaS Valuation Engine

A Python tool that values 18 US-listed SaaS companies with trading comps and a DCF model, then ranks which look under- or overvalued.

> Status: **Stage 4 (DCF model) complete.** Ranking is next.

## Overview

_TODO (Stage 8): the investment question, the approach, and headline findings._

## Methodology

**Comps (Stage 3).** EV/Revenue, EV/EBITDA and P/FCF alongside growth, margins, Rule of 40, SBC and net dilution. Flow metrics are calendarized to a common calendar year, since fiscal years end anywhere from January to December. Results that are mathematically valid but meaningless, such as a multiple of negative EBITDA, are shown as NM. Missing inputs are shown as n/a with the reason recorded. The full definitions are in the workbook's Methodology sheet.

**Data integrity.** Stock splits are detected automatically and every pre-split share count is restated to today's basis, so a split is never mistaken for dilution. Numbers missing from the SEC's standardized dataset are entered by hand in `config.yaml` with a cited source, never estimated.

**DCF (Stage 4).** A two-stage free cash flow model: 5 explicit years, then 5 fade years in which revenue growth glides down to a 3% terminal rate and FCF margins converge to a mature level. A plain 5-year model that forces growth to 3% by year 5 gives every company the same ~3-4x terminal revenue multiple and erases the premium for high growth; the fade stage fixes that. Other choices:
- The discount rate is WACC, with the cost of equity from CAPM using Blume-adjusted betas.
- Mid-year discounting and a Gordon growth terminal value.
- Stock-based comp is treated as a real expense, since it's employee pay delivered in shares.
- Each company sheet includes a WACC × terminal growth sensitivity grid.
- A **reverse DCF** solves for the starting growth that today's price implies, which separates "the model is biased" from "the market expects more".

_TODO: ranking logic._

## Data sources

- **SEC EDGAR XBRL API** (`companyfacts`): historical financials from 10-K filings
- **yfinance**: share prices, market cap, shares outstanding

## Setup

```bash
git clone <repo-url>
cd saas-valuation-engine
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,dashboard]"
cp .env.example .env   # then add your name and email
```

## Usage

```bash
python -m saas_valuation.data.pipeline            # fetch + clean data (uses cache when fresh)
python -m saas_valuation.data.pipeline --refresh  # force re-download
python -m saas_valuation.models.comps             # comps -> outputs/comps_CY2025.xlsx
python -m saas_valuation.models.dcf               # DCF   -> outputs/dcf_models.xlsx
pytest                                             # run tests
```

The pipeline writes to `data/processed/`:

| File | Contents |
|---|---|
| `financials.csv` | One row per company per fiscal year, one column per metric |
| `financials_long.csv` | Same data in long format, with the XBRL tag each number came from |
| `market_data.csv` | Price, market cap, share counts, beta |
| `data_quality_report.csv` | Every gap or inconsistency found, ranked error / warning / info / resolved |
| `stock_splits.csv` | Every split on record and how many share counts it restated |
| `overrides_log.csv` | Hand-entered values: applied or still pending |

## Project structure

```
config/config.yaml       All assumptions (universe, WACC inputs, ranking weights)
data/raw/                Cached API responses (not committed)
data/processed/          Cleaned financials (not committed)
src/saas_valuation/      Package: data/, models/, utils/
tests/                   Unit tests
dashboard/               Streamlit app
```

## Roadmap

- [x] 1. Setup
- [x] 2. Data pipeline
- [x] 3. Comps analysis
- [x] 4. DCF model
- [ ] 5. Ranking
- [ ] 6. Streamlit dashboard
- [ ] 7. AI risk summaries (optional)
- [ ] 8. Polish

## Disclaimer

Educational project. Not investment advice.
