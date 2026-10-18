"""Tests for 10-K Item 1A extraction, including the traps found in real filings."""

from saas_valuation.data.filings import extract_item_1a, html_to_text, latest_10k

BODY = "Our business faces many risks. " * 50


def test_skips_table_of_contents_and_cross_references():
    text = (
        "Item 1A. Risk Factors 12\nItem 1B. Unresolved Staff Comments 40\nItem 2. Properties 41\n"
        'A summary follows; see "Part I, Item 1A. Risk Factors" for details.\n'
        "Item 1. Business\nWe sell software. " * 3
        + "\nITEM 1A. RISK FACTORS\n" + BODY
        + "\nItem 1B. Unresolved Staff Comments\nNone.\nItem 2. Properties\nOffices."
    )
    section = extract_item_1a(text)
    assert section.startswith("ITEM 1A. RISK FACTORS")
    assert "We sell software" not in section          # the cross-reference trap
    assert section.rstrip().endswith("faces many risks.")


def test_stops_at_cybersecurity_item_when_1b_is_absent():
    text = "Item 1A Risk Factors\n" + BODY + "\nItem 1C. Cybersecurity\nWe have a program."
    assert "Cybersecurity" not in extract_item_1a(text)


def test_html_to_text_drops_hidden_xbrl_and_keeps_paragraphs():
    html = ("<html><body><ix:header><div>hidden-metadata</div></ix:header>"
            "<p>Item&nbsp;1A.&#160;Risk Factors</p><p>First &amp; second.</p></body></html>")
    assert html_to_text(html) == "Item 1A. Risk Factors\nFirst & second."


def test_latest_10k_ignores_other_forms():
    subs = {"filings": {"recent": {"form": ["10-Q", "8-K", "10-K", "10-K"],
                                   "accessionNumber": ["a", "b", "c", "d"],
                                   "filingDate": ["2026-06-01", "2026-05-01", "2026-03-01", "2025-03-01"],
                                   "reportDate": ["", "", "2026-01-31", "2025-01-31"],
                                   "primaryDocument": ["q.htm", "k.htm", "10k.htm", "old.htm"]}}}
    assert latest_10k(subs)["accession"] == "c"
