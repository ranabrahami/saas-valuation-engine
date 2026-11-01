"""Fetch each company's latest 10-K and extract Item 1A (Risk Factors) as plain text.

Item 1A is where a company must disclose the risks it considers material.
It's long (often 30+ pages) and partly boilerplate, which is why Stage 7
asks Claude to rank and summarize it.

The tricky part is finding the section. "Item 1A. Risk Factors" appears in
three kinds of places:
  * the table of contents        (a heading line, followed shortly by Item 1B)
  * cross-references in the text ('see "Item 1A. Risk Factors"', mid-sentence)
  * the real section start       (a heading line, followed by ~30 pages)
Headings sit at the start of a line; cross-references don't. So only
line-start matches count, and of those we keep the longest span up to the
next section heading (Item 1B / 1C / 2).
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from .sec_client import SecClient

_SEP = r"\s*[\.:\-–—]?\s*"
START = re.compile(rf"^\s*item\s*1a{_SEP}risk\s+factors", re.IGNORECASE | re.MULTILINE)
END = re.compile(rf"^\s*item\s*1b{_SEP}unresolved\s+staff\s+comments"
                 rf"|^\s*item\s*1c{_SEP}cybersecurity"
                 rf"|^\s*item\s*2{_SEP}properties", re.IGNORECASE | re.MULTILINE)
MIN_WORDS = 3000  # anything shorter means extraction probably went wrong

BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
SKIP_TAGS = {"script", "style", "ix:header", "head"}


class _TextExtractor(HTMLParser):
    """HTML -> text, keeping paragraph breaks and skipping hidden XBRL metadata."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def extract_item_1a(text: str) -> str | None:
    """The longest span from an 'Item 1A. Risk Factors' heading to the next section heading."""
    best = None
    for start in START.finditer(text):
        end = END.search(text, start.end())
        if end is None:
            continue
        section = text[start.start():end.start()].strip()
        if best is None or len(section) > len(best):
            best = section
    return best


def latest_10k(submissions: dict) -> dict | None:
    recent = submissions["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form == "10-K":
            return {"accession": recent["accessionNumber"][i], "filing_date": recent["filingDate"][i],
                    "report_date": recent["reportDate"][i], "document": recent["primaryDocument"][i]}
    return None


def fetch_risk_factors(client: SecClient, ticker: str, cik: int, cache_dir: Path) -> dict:
    """Latest 10-K's Item 1A for one company, with metadata and a quality check."""
    filing = latest_10k(client.submissions(cik))
    if filing is None:
        return {"ticker": ticker, "status": "error", "detail": "No 10-K in recent filings."}

    stem = f"{ticker}_{filing['accession']}"
    text_file = cache_dir / f"{stem}_item1a.txt"
    if text_file.exists():
        section = text_file.read_text()
    else:
        html = client.filing_document(cik, filing["accession"], filing["document"], cache_dir / f"{stem}.htm")
        section = extract_item_1a(html_to_text(html)) or ""
        if section:
            text_file.write_text(section)

    words = len(section.split())
    status, detail = "ok", ""
    if not section:
        status, detail = "error", "Could not locate Item 1A in the 10-K."
    elif words < MIN_WORDS:
        status, detail = "warning", f"Only {words:,} words; the section may be cut short or incorporated by reference."
    url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
           f"{filing['accession'].replace('-', '')}/{filing['document']}")
    return {"ticker": ticker, **filing, "url": url, "text": section, "words": words,
            "status": status, "detail": detail}


# Item 1 (Business): from its heading to the Item 1A heading. Line-anchored like
# Item 1A, so table-of-contents lines and in-text cross-references are skipped.
ITEM1_START = re.compile(rf"^\s*item\s*1{_SEP}business\b", re.IGNORECASE | re.MULTILINE)
ITEM1_END = re.compile(rf"^\s*item\s*1a{_SEP}risk\s+factors", re.IGNORECASE | re.MULTILINE)


def extract_item_1(text: str) -> str | None:
    """The longest span from an 'Item 1. Business' heading to the next 'Item 1A' heading."""
    best = None
    for start in ITEM1_START.finditer(text):
        end = ITEM1_END.search(text, start.end())
        if end is None:
            continue
        section = text[start.start():end.start()].strip()
        if best is None or len(section) > len(best):
            best = section
    return best


def business_section(cache_dir: Path, ticker: str, accession: str, max_words: int | None = None) -> str | None:
    """Item 1 from an already-downloaded 10-K (cached by the risk layer), trimmed to max_words."""
    stem = f"{ticker}_{accession}"
    text_file = cache_dir / f"{stem}_item1.txt"
    if text_file.exists():
        section = text_file.read_text()
    else:
        html_file = cache_dir / f"{stem}.htm"
        if not html_file.exists():
            return None
        section = extract_item_1(html_to_text(html_file.read_text(errors="ignore"))) or ""
        if section:
            text_file.write_text(section)
    if not section:
        return None
    words = section.split()
    return section if max_words is None or len(words) <= max_words else " ".join(words[:max_words]) + " [...]"
