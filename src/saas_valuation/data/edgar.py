"""Turns a raw SEC companyfacts JSON into a clean annual time series.

Main pitfalls this handles:
  1. Duplicates: each 10-K repeats the prior 2 years as comparatives, so the
     same number appears in several filings. We keep the most recently filed
     version, which also picks up any restatements.
  2. Quarters mixed with years: companyfacts includes 10-Q data. We keep only
     10-K facts whose period is ~1 year long.
  3. Fiscal-year labels: the "fy" field in companyfacts is the year of the
     FILING, not of the number, so it's unreliable. We key everything by
     the period END date and label the fiscal year as that date's year
     (e.g. Salesforce's year ending Jan 31, 2025 = FY2025, which matches
     how the company names it).
  4. Inconsistent tags: handled with the priority lists in tags.py.
"""

from datetime import date

from .tags import CONCEPTS, Concept

ANNUAL_FORMS = {"10-K", "10-K/A"}


def _tag_facts(facts: dict, tag: str, unit: str) -> list[dict]:
    return facts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get(unit, [])


def _annual_values(facts: dict, tag: str, concept: Concept, min_days: int,
                   max_days: int) -> dict[date, dict]:
    """Return {period_end: fact} for one tag, annual 10-K data only, latest filing wins."""
    best: dict[date, dict] = {}
    for fact in _tag_facts(facts, tag, concept.unit):
        if fact.get("form") not in ANNUAL_FORMS:
            continue
        has_start = "start" in fact
        if concept.period == "duration":
            if not has_start:
                continue
            days = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
            if not min_days <= days <= max_days:
                continue
        elif has_start:
            continue

        end = date.fromisoformat(fact["end"])
        if end not in best or fact["filed"] > best[end]["filed"]:
            best[end] = fact
    return best


def _issue(ticker, severity, issue, detail, concept="", fiscal_year=None) -> dict:
    return {"ticker": ticker, "severity": severity, "concept": concept,
            "fiscal_year": fiscal_year, "issue": issue, "detail": detail}


def fiscal_period_ends(facts: dict, min_days: int, max_days: int, years: int) -> list[date]:
    """The company's last N fiscal year-end dates, found via its annual revenue facts."""
    revenue = next(c for c in CONCEPTS if c.name == "revenue")
    ends: set[date] = set()
    for tag in revenue.tags:
        ends.update(_annual_values(facts, tag, revenue, min_days, max_days))
    return sorted(ends)[-years:]


def extract_company(ticker: str, facts: dict, min_days: int, max_days: int,
                    years: int, concepts: tuple[Concept, ...] = CONCEPTS) -> tuple[list[dict], list[dict]]:
    """Extract every concept for each of the last `years` fiscal years.

    Returns (records, issues). Records are long-format rows with the tag
    used, so every number can be traced back to its source.
    """
    records: list[dict] = []
    issues: list[dict] = []

    ends = fiscal_period_ends(facts, min_days, max_days, years)
    if not ends:
        issues.append(_issue(ticker, "error", "no_annual_revenue",
                             "No annual 10-K revenue found under any known tag."))
        return records, issues

    if len(ends) < years:
        issues.append(_issue(ticker, "warning", "short_history",
                             f"Only {len(ends)} of {years} fiscal years available "
                             f"(first: FY{ends[0].year}). Recent IPO or change of filer status?"))

    for earlier, later in zip(ends, ends[1:]):
        gap = (later - earlier).days
        if not 350 <= gap <= 380:
            issues.append(_issue(ticker, "warning", "irregular_year_gap",
                                 f"{gap} days between FY ends {earlier} and {later}. "
                                 "Possible missing year or fiscal year-end change."))

    if ends[-1].month != 12:
        issues.append(_issue(ticker, "info", "non_december_fye",
                             f"Fiscal year ends in month {ends[-1].month}. Comps against "
                             "December filers will need calendarization (Stage 3)."))

    for concept in concepts:
        per_tag = {tag: _annual_values(facts, tag, concept, min_days, max_days)
                   for tag in concept.tags}
        previous_tag = None
        for end in ends:
            hit = next(((tag, vals[end]) for tag, vals in per_tag.items() if end in vals), None)
            if hit is None:
                issues.append(_issue(ticker, concept.missing_severity, "missing",
                                     f"No value under any of {len(concept.tags)} known tags. "
                                     f"{concept.description}",
                                     concept.name, end.year))
                continue

            tag, fact = hit
            records.append({
                "ticker": ticker, "fiscal_year": end.year, "period_end": end.isoformat(),
                "concept": concept.name, "value": fact["val"], "tag": tag,
                "form": fact["form"], "filed": fact["filed"],
            })
            if previous_tag and tag != previous_tag:
                issues.append(_issue(ticker, "info", "tag_changed",
                                     f"Tag switched from {previous_tag} to {tag}. "
                                     "Check the definitions match.",
                                     concept.name, end.year))
            previous_tag = tag

    debt_concepts = {c.name for c in concepts if c.name.startswith("debt_")}
    if debt_concepts and not any(r["concept"] in debt_concepts for r in records):
        issues.append(_issue(ticker, "warning", "no_debt_found",
                             "No debt under any known tag in any year. Consistent with a "
                             "debt-free company, but confirm in the 10-K before treating debt as 0.",
                             "debt"))

    issues.extend(consistency_checks(ticker, records))
    return records, issues


def cover_page_shares(facts: dict) -> tuple[float | None, str | None]:
    """Latest shares outstanding from a filing's cover page (dei taxonomy).

    More current than the 10-K's weighted-average count, since every 10-Q
    updates it. Unavailable for most dual-class companies: per-class counts
    are dimensional data, which companyfacts leaves out.
    """
    rows = (facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {})
            .get("units", {}).get("shares", []))
    if not rows:
        return None, None
    latest = max(rows, key=lambda r: (r["end"], r["filed"]))
    return latest["val"], latest["end"]


def consistency_checks(ticker: str, records: list[dict]) -> list[dict]:
    """Cross-check numbers that should agree with each other."""
    issues = []
    by_year: dict[int, dict[str, float]] = {}
    for r in records:
        by_year.setdefault(r["fiscal_year"], {})[r["concept"]] = r["value"]

    for fy, v in sorted(by_year.items()):
        if v.get("revenue", 1) <= 0:
            issues.append(_issue(ticker, "error", "non_positive_revenue",
                                 f"Revenue is {v['revenue']:,.0f}.", "revenue", fy))

        if {"revenue", "cost_of_revenue", "gross_profit"} <= v.keys():
            derived = v["revenue"] - v["cost_of_revenue"]
            if abs(derived - v["gross_profit"]) > 0.01 * abs(v["revenue"]):
                issues.append(_issue(ticker, "warning", "gross_profit_mismatch",
                                     f"Tagged gross profit {v['gross_profit']:,.0f} vs "
                                     f"revenue - cost of revenue {derived:,.0f}.",
                                     "gross_profit", fy))

        if v.get("capex", 0) < 0:
            issues.append(_issue(ticker, "warning", "negative_capex",
                                 "Capex reported as negative; sign convention differs from other filers.",
                                 "capex", fy))
    return issues
