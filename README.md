# SaaS Valuation Engine

A Python tool that values 18 US-listed SaaS companies with trading comps and a DCF model, then ranks which look under- or overvalued.

> Status: **Stage 1 (setup) complete.** Data pipeline is next.

## Overview

_TODO (Stage 8): the investment question, the approach, and headline findings._

## Methodology

_TODO: comps metrics, DCF approach, ranking logic. Filled in as each stage is built._

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
- [ ] 2. Data pipeline
- [ ] 3. Comps analysis
- [ ] 4. DCF model
- [ ] 5. Ranking
- [ ] 6. Streamlit dashboard
- [ ] 7. AI risk summaries (optional)
- [ ] 8. Polish

## Disclaimer

Educational project. Not investment advice.
