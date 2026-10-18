"""Thin HTTP client for SEC EDGAR, with rate limiting and a local file cache.

Two endpoints are used:
  * company_tickers.json  -> maps ticker symbols to CIK numbers (the SEC's
                             permanent company ID; tickers can change, CIKs don't)
  * companyfacts          -> every XBRL-tagged number a company has ever filed
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path, max_requests_per_second: float,
                 cache_max_age_days: float):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = 1.0 / max_requests_per_second
        self.cache_max_age = timedelta(days=cache_max_age_days)
        self._last_request = 0.0

    def _get_json(self, url: str, cache_file: Path, refresh: bool) -> dict:
        if not refresh and cache_file.exists():
            age = datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
            if age < self.cache_max_age:
                return json.loads(cache_file.read_text())

        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        response = self.session.get(url, timeout=30)
        self._last_request = time.monotonic()
        response.raise_for_status()

        data = response.json()
        cache_file.write_text(json.dumps(data))
        return data

    def ticker_to_cik(self, refresh: bool = False) -> dict[str, int]:
        raw = self._get_json(TICKER_MAP_URL, self.cache_dir / "company_tickers.json", refresh)
        return {row["ticker"].upper(): int(row["cik_str"]) for row in raw.values()}

    def company_facts(self, cik: int, refresh: bool = False) -> dict:
        url = COMPANY_FACTS_URL.format(cik=cik)
        return self._get_json(url, self.cache_dir / f"companyfacts_CIK{cik:010d}.json", refresh)
