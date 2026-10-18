"""Adjustments layered on top of the raw SEC data, each leaving an audit trail.

* Stock splits: after a 4-for-1 split, each old share becomes 4 new ones.
  The company's value doesn't change, but share counts filed before the
  split are on a different basis than today's count and price. Comparing
  them unadjusted would show 300% "dilution" that never happened.
* Overrides: hand-entered values from the 10-K for lines the SEC's
  standardized dataset doesn't carry.
"""

import pandas as pd

from .tags import CONCEPTS

SHARE_CONCEPTS = {c.name for c in CONCEPTS if c.unit == "shares"}


def split_factor(splits: list[dict], filed: str | None) -> float:
    """Multiplier that converts a share count filed on `filed` to today's basis.

    Only splits AFTER the filing date apply: a 10-K filed after a split
    already reports restated, post-split counts for every year it shows.
    """
    if not filed:
        return 1.0
    factor = 1.0
    for split in splits:
        if split["date"] > filed:
            factor *= split["ratio"]
    return factor


def add_split_adjusted_shares(wide: pd.DataFrame, long: pd.DataFrame,
                              splits: dict[str, list[dict]]) -> pd.DataFrame:
    """Add split_factor and <concept>_split_adj columns for every share-count concept."""
    wide = wide.copy()
    for concept in SHARE_CONCEPTS & set(wide.columns):
        rows = long[long["concept"] == concept][["ticker", "fiscal_year", "filed"]].copy()
        rows["split_factor"] = [split_factor(splits.get(t, []), f)
                                for t, f in zip(rows["ticker"], rows["filed"])]
        wide = wide.merge(rows[["ticker", "fiscal_year", "split_factor"]],
                          on=["ticker", "fiscal_year"], how="left")
        wide["split_factor"] = wide["split_factor"].fillna(1.0)
        wide[f"{concept}_split_adj"] = wide[concept] * wide["split_factor"]
    return wide


def apply_overrides(records: list[dict], issues: list[dict],
                    overrides: list[dict]) -> tuple[list[dict], list[dict]]:
    """Apply hand-entered values; returns (records, override log).

    Mutates matching "missing" issues to severity "resolved" so the quality
    report shows both the original gap and how it was closed.
    """
    log = []
    period_end = {(r["ticker"], r["fiscal_year"]): r["period_end"] for r in records}

    for o in overrides or []:
        key = (o["ticker"], o["fiscal_year"], o["concept"])
        entry = {"ticker": o["ticker"], "fiscal_year": o["fiscal_year"], "concept": o["concept"],
                 "value_usd_millions": o.get("value_usd_millions"), "source": o.get("source", "")}

        if o.get("value_usd_millions") is None:
            log.append({**entry, "status": "pending", "previous_value": None})
            continue
        if (o["ticker"], o["fiscal_year"]) not in period_end:
            log.append({**entry, "status": "unmatched: no data for that ticker/year", "previous_value": None})
            continue

        value = o["value_usd_millions"] * 1e6
        existing = next((r for r in records if (r["ticker"], r["fiscal_year"], r["concept"]) == key), None)
        previous = existing["value"] if existing else None
        if existing:
            records.remove(existing)
        records.append({"ticker": o["ticker"], "fiscal_year": o["fiscal_year"],
                        "period_end": period_end[(o["ticker"], o["fiscal_year"])],
                        "concept": o["concept"], "value": value, "tag": "override",
                        "form": "manual", "filed": None, "source": entry["source"]})
        log.append({**entry, "status": "applied", "previous_value": previous})

        for issue in issues:
            if (issue["ticker"], issue["fiscal_year"], issue["concept"]) == key and issue["issue"] == "missing":
                issue["severity"] = "resolved"
                issue["detail"] += f" Resolved by override: {entry['source']}"
    return records, log
