"""Loads the dashboard in a real browser and fails on any JavaScript error.

AppTest (test_dashboard.py) runs the Python but never draws the charts, so an
invalid Vega-Lite spec renders as a blank chart without failing anything. This
test catches that. Skipped unless Playwright and Google Chrome are available.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not (ROOT / "data" / "processed" / "comps.csv").exists(),
                                reason="run the pipeline and comps first")
TABS = ["Ranking", "Company", "Comps", "Data & method"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_url():
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard" / "app.py"),
         "--server.headless", "true", "--server.port", str(port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            socket.create_connection(("localhost", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.5)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def test_every_tab_renders_without_browser_errors(app_url):
    errors = []
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception as exc:  # Chrome not installed
            pytest.skip(f"Google Chrome unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(app_url)
        page.wait_for_selector('[data-testid="stTabs"]', timeout=60_000)
        for tab in TABS:
            page.get_by_role("tab", name=tab, exact=True).click()
            page.wait_for_timeout(2_000)
        charts = page.locator('[data-testid="stVegaLiteChart"]').count()
        browser.close()
    assert not errors, errors
    assert charts >= 4
