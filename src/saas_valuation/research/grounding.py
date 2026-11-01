"""Check every number in generated text against the numbers it was given.

The narrative may only use numbers from the fact sheet (tagged [F12]), the 10-K
excerpt, or the risk summary. Every number in the text is parsed and matched at
the precision it is written ("$3.1B" matches 3,130,000,000; "19%" matches
0.1924). Fact tags must name a fact that exists. Ported from the Valuation
Tutor project, where it kept a tutor from inventing figures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NUMBER = re.compile(
    r"(?P<neg>[-−])?(?P<cur>[$])?(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"\s?(?P<unit>%|x\b|times\b|bn\b|B\b|M\b|K\b|billion\b|million\b|percentage points?\b|points?\b|pts?\b|bps\b)?",
    re.IGNORECASE)
TAG = re.compile(r"\[(F\d+)\]")
YEAR = re.compile(r"^(?:FY|CY)?(19|20)\d\d[E]?$")
SCALE = {"b": 1e9, "bn": 1e9, "billion": 1e9, "m": 1e6, "million": 1e6, "k": 1e3}


@dataclass(frozen=True)
class Num:
    shown: float
    scale: float
    decimals: int
    kind: str           # "pct", "multiple", "money", "plain"
    text: str

    @property
    def value(self) -> float:
        return self.shown * self.scale

    def matches(self, candidate: float) -> bool:
        return abs(candidate / self.scale - self.shown) <= 0.5 * 10 ** -self.decimals + 1e-9


def parse(text: str) -> list[Num]:
    out = []
    for m in NUMBER.finditer(text):
        dec = m.group("dec") or ""
        shown = float(m.group("num").replace(",", "") + dec) * (-1 if m.group("neg") else 1)
        decimals = max(len(dec) - 1, 0)
        unit = (m.group("unit") or "").lower()
        if unit == "%" or unit.startswith("percentage") or unit in ("pt", "pts", "point", "points"):
            kind, scale = "pct", 0.01
        elif unit == "bps":
            kind, scale = "pct", 0.0001
        elif unit in ("x", "times"):
            kind, scale = "multiple", 1.0
        elif unit in SCALE or m.group("cur"):
            kind, scale = "money", SCALE.get(unit, 1.0)
        else:
            kind, scale = "plain", 1.0
        out.append(Num(shown, scale, decimals, kind, m.group(0)))
    return out


@dataclass
class Grounding:
    ok: bool
    unverified: list[str] = field(default_factory=list)
    bad_tags: list[str] = field(default_factory=list)


def check(text: str, facts: dict[str, tuple[float, str]], sources: list[str]) -> Grounding:
    """facts: tag -> (value in base units, kind). sources: texts whose numbers may be repeated."""
    allowed = [(kind, v) for v, kind in facts.values()] + [(n.kind, n.value) for s in sources for n in parse(s)]
    # Differences between two facts are fine ("trails the median by 15 points"), within tolerance.
    pct = [v for k, v in allowed if k == "pct"]
    allowed += [("pct", abs(a - b)) for i, a in enumerate(pct) for b in pct[i + 1:]]
    unverified = []
    clean = TAG.sub("", text)
    for n in parse(clean):
        token = n.text.strip()
        if n.kind == "plain" and (YEAR.match(token) or (n.shown.is_integer() and 0 <= n.shown <= 12)):
            continue                         # years and small counts ("three pillars", "5 years")
        kinds = {n.kind, "multiple"} if n.kind == "plain" else {n.kind}
        if not any(k in kinds and n.matches(v) for k, v in allowed):
            unverified.append(token)
    bad = [t for t in TAG.findall(text) if t not in facts]
    return Grounding(not unverified and not bad, unverified, bad)
