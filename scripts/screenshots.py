"""Capture the README screenshots from a running dashboard.

Usage:
    streamlit run dashboard/app.py          # in one terminal
    python scripts/screenshots.py           # in another; writes docs/images/*.png

Needs `pip install playwright`. Uses the locally installed Google Chrome, so no
browser download is required (pass --bundled to use Playwright's Chromium instead).
"""

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
SETTLE_MS = 2_500  # Vega charts draw (and animate) after Streamlit finishes the run


def wait_for_app(page: Page) -> None:
    """Wait until Streamlit has finished running the script."""
    page.wait_for_selector('[data-testid="stApp"]')
    page.wait_for_function(
        "() => !document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=60_000)
    page.wait_for_timeout(SETTLE_MS)


def open_tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name, exact=True).click()
    page.wait_for_timeout(SETTLE_MS)


def section(page: Page, heading: str, path: Path, height: int) -> None:
    """Screenshot the main column from a heading down, so the title and caption explain the image."""
    target = page.get_by_role("heading", name=heading).locator("visible=true").first
    target.evaluate("el => el.scrollIntoView({block: 'start'})")
    page.mouse.move(0, 0)  # hovering a chart shows its toolbar
    page.wait_for_timeout(SETTLE_MS)
    head = target.bounding_box()
    main = page.locator('[data-testid="stMainBlockContainer"]').bounding_box()
    page.screenshot(path=path, clip={"x": main["x"], "y": max(head["y"] - 16, 0),
                                     "width": main["width"], "height": height})


def main(url: str, bundled: bool) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(**({} if bundled else {"channel": "chrome"}))
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1.5)
        page.goto(url)
        wait_for_app(page)

        # Ranking: headline metrics, the live-assumption sidebar and the ranked table.
        page.screenshot(path=OUT / "ranking.png")
        section(page, "Do the two methods agree?", OUT / "quadrant.png", 560)
        if page.get_by_role("heading", name="Cheap for a reason?").count():
            section(page, "Cheap for a reason?", OUT / "ai_risk.png", 480)

        # Company deep dive for the top-ranked company (first in the selector).
        page.evaluate("document.querySelector('[data-testid=\"stMain\"]').scrollTo(0, 0)")
        open_tab(page, "Company")
        page.screenshot(path=OUT / "company.png")
        if page.get_by_role("heading", name="Risk factors from the latest 10-K").count():
            section(page, "Risk factors from the latest 10-K", OUT / "company_risk.png", 760)

        # Comps regression.
        page.evaluate("document.querySelector('[data-testid=\"stMain\"]').scrollTo(0, 0)")
        open_tab(page, "Comps")
        section(page, "EV / Revenue vs", OUT / "comps_regression.png", 600)
        browser.close()
    for f in sorted(OUT.glob("*.png")):
        print(f"wrote {f.relative_to(OUT.parents[1])}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8501")
    parser.add_argument("--bundled", action="store_true", help="use Playwright's Chromium, not local Chrome")
    args = parser.parse_args()
    main(args.url, args.bundled)
