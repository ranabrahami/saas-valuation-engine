"""The report narrative writer, with a fake Claude client (nothing billed)."""

import json
from types import SimpleNamespace

import pytest

from saas_valuation.config import load_config, project_path
from saas_valuation.research import grounding

CFG = load_config()
DATA = (project_path(CFG["data"]["processed_dir"]) / "comps.csv").exists()


@pytest.mark.parametrize("text,ok", [
    ("Revenue was $3.1B [F1], up 19% [F2].", True),
    ("The multiple of 3.1x [F3] compares with 8.0x for peers.", False),       # 8.0x not given
    ("We see 25 points of upside versus the median.", True),                   # a difference of two facts
    ("Reaffirm three pillars; FY2025 revenue [F1] grew.", True),               # years and small counts
    ("Target of $400 [F9].", False),                                           # unknown tag and number
    ("Near its 52-week high, with a Rule of 40 score below peers, per the 10-K.", True),   # names, not data
    ("The 25-point gap is the opportunity.", True),                             # hyphenated unit
    ("A 99-point gap.", False),
])
def test_grounding(text, ok):
    facts = {"F1": (3.13e9, "money"), "F2": (0.1924, "pct"), "F3": (3.08, "multiple"),
             "F4": (0.40, "pct"), "F5": (0.15, "pct")}
    assert grounding.check(text, facts, []).ok is ok


class _Ctx:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return SimpleNamespace(get_final_message=lambda: self.message)

    def __exit__(self, *exc):
        return False


def fake(*payloads):
    """A stand-in for anthropic.Anthropic: client.beta.messages.stream(...) returns the queued payloads."""
    client = SimpleNamespace(replies=list(payloads), requests=[])

    def stream(**request):
        client.requests.append(request)
        payload = client.replies.pop(0)
        msg = SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                              stop_reason="end_turn", model="claude-opus-5-5",
                              usage=SimpleNamespace(input_tokens=12000, output_tokens=3000))
        return _Ctx(msg)
    client.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    return client


def draft(c, summary_extra="", body="Revenue grew steadily."):
    t = c.target
    return {"headline": "A steady compounder at a discount",
            "summary": f"We initiate at {t['rating']} with a ${t['price_target']:,.0f} price target. {summary_extra}",
            "thesis": [{"title": f"Pillar {i}", "text": body} for i in range(3)],
            "business_overview": "It sells software subscriptions.", "valuation": body,
            "risks": [{"title": "Competition", "text": body}], "what_would_change": ["Growth slows", "a", "b"]}


@pytest.fixture(scope="module")
def company():
    if not DATA:
        pytest.skip("run the pipeline and comps first")
    from saas_valuation.research.data import load_universe
    return load_universe().company("ADBE")


def test_request_carries_rating_facts_and_rules(company):
    from saas_valuation.research.narrative import SCHEMA, build_request
    req = build_request(company, "Adobe sells creative software.", CFG)
    user = req["messages"][0]["content"]
    assert f"Rating: {company.target['rating']}" in user and "[F2] 12-month price target" in user
    assert "never change, soften or contradict" in req["system"]
    assert req["output_config"]["format"]["schema"] == SCHEMA and req["model"] == CFG["research"]["model"]


def test_grounded_draft_is_accepted(company):
    from saas_valuation.research.narrative import write
    res = write(fake(draft(company)), company, "Adobe sells creative software.", CFG)
    assert res["status"] == "ok" and res["attempts"] == 1 and res["cost_usd"] > 0


def test_invented_number_gets_one_retry_then_needs_review(company):
    from saas_valuation.research.narrative import write
    bad = draft(company, body="Revenue will reach $99B.")
    client = fake(bad, draft(company))
    res = write(client, company, "Adobe sells creative software.", CFG)
    assert res["status"] == "ok" and res["attempts"] == 2
    assert "$99B" in client.requests[1]["messages"][-1]["content"]
    res = write(fake(bad, bad), company, "Adobe sells creative software.", CFG)
    assert res["status"] == "needs_review" and any("$99B" in p for p in res["problems"])


def test_summary_must_state_the_models_rating_and_target(company):
    from saas_valuation.research.narrative import check
    d = draft(company)
    d["summary"] = "We like the company."
    problems = check(d, company, "")["problems"]
    assert any("rating" in p for p in problems) and any("price target" in p for p in problems)


def test_numbers_quoted_from_the_10k_are_allowed(company):
    from saas_valuation.research.narrative import check
    d = draft(company, body="It serves over 4,000 enterprise customers.")
    assert check(d, company, "We serve over 4,000 enterprise customers worldwide.")["ok"]


def test_fact_labels_name_the_year_and_targets_are_whole_dollars(company):
    from saas_valuation.research.narrative import show, tagged
    labels = [label for label, *_ in tagged(company).values()]
    assert not any("(CY)" in lb for lb in labels)
    assert any(f"calendar {company.calendar_year}" in lb for lb in labels)
    assert show(388.0, "money") == "$388" and show(250.23, "money") == "$250.23"


def test_first_attempt_problems_are_logged(company):
    from saas_valuation.research.narrative import write
    bad = draft(company, body="Revenue will reach $99B.")
    res = write(fake(bad, draft(company)), company, "", CFG)
    assert any("$99B" in p for p in res["first_attempt_problems"])
