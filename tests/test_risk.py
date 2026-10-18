"""Tests for the AI risk layer, using a fake client so nothing is billed."""

import json
from types import SimpleNamespace

import pytest

from saas_valuation.config import load_config
from saas_valuation.models.risk import SCHEMA, build_request, cost, quote_found, summarize

SOURCE = ("Item 1A. Risk Factors\nIf customers adopt AI agents that perform work previously done by "
          "employees, demand for per-seat subscriptions could decline. We face intense competition.")
FILING = {"text": SOURCE, "filing_date": "2026-02-11", "report_date": "2025-12-31",
          "accession": "0001-26-1", "url": "https://sec.gov/x", "words": 30}


class FakeClient:
    """Mimics client.beta.messages.stream(...) and records the request it received."""

    def __init__(self, payload: dict | None, stop_reason: str = "end_turn"):
        self.payload, self.stop_reason, self.request = payload, stop_reason, None
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **request):
        self.request = request
        content = [SimpleNamespace(type="thinking", thinking="")]
        if self.payload is not None:
            content.append(SimpleNamespace(type="text", text=json.dumps(self.payload)))
        message = SimpleNamespace(content=content, stop_reason=self.stop_reason, model="claude-opus-5-5",
                                  usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
        return _Ctx(message)


class _Ctx:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return SimpleNamespace(get_final_message=lambda: self.message)

    def __exit__(self, *exc):
        return False


def payload(ai_quote: str, risk_quote: str) -> dict:
    return {"summary": "Seat-based demand is exposed to AI agents.",
            "top_risks": [{"title": "AI agents reduce seats", "category": "ai_disruption", "severity": "high",
                           "company_specific": True, "explanation": "Fewer seats, less revenue.",
                           "quote": risk_quote}],
            "ai_disruption": {"level": "high", "mechanisms": ["seat_compression"],
                              "assessment": "Directly tied to the seat model.", "quote": ai_quote}}


@pytest.fixture
def cfg():
    return load_config()


def test_quote_check_tolerates_formatting_but_not_paraphrase():
    assert quote_found("demand for per-seat subscriptions could decline", SOURCE)
    assert quote_found("DEMAND for per‑seat   subscriptions could decline.", SOURCE.replace("-", "‑"))
    assert not quote_found("demand for seats might fall", SOURCE)
    assert not quote_found("", SOURCE)


def test_request_uses_schema_effort_and_fallbacks(cfg):
    client = FakeClient(payload("intense competition", "intense competition"))
    summarize(client, "HubSpot", "HUBS", FILING, cfg)
    r = client.request
    assert r["model"] == cfg["ai_risk"]["model"]
    assert r["output_config"]["format"]["schema"] == SCHEMA
    assert r["output_config"]["effort"] == cfg["ai_risk"]["effort"]
    assert r["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in r["betas"]
    assert SOURCE in r["messages"][0]["content"]


def test_parses_result_and_flags_unverified_quotes(cfg):
    client = FakeClient(payload("demand for per-seat subscriptions could decline", "AI will destroy us"))
    result = summarize(client, "HubSpot", "HUBS", FILING, cfg)
    assert result["status"] == "ok"
    assert result["ai_disruption"]["quote_verified"] is True
    assert result["top_risks"][0]["quote_verified"] is False
    assert result["cost_usd"] == pytest.approx(cost({"input_tokens": 1000, "output_tokens": 500}, cfg["ai_risk"]))


def test_refusal_and_truncation_are_reported_not_parsed(cfg):
    assert summarize(FakeClient(None, "refusal"), "X", "X", FILING, cfg)["status"] == "refused"
    assert summarize(FakeClient(None, "max_tokens"), "X", "X", FILING, cfg)["status"] == "truncated"


def test_schema_is_strict():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])
    assert build_request("A", "A", FILING, load_config())["system"]
