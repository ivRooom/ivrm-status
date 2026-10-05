"""Regression tests for the second round of review findings on the budget guard."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from ops_agent.agent import analyze, build_request
from ops_agent.budget import BudgetExceeded, BudgetLedger, cost_jpy
from ops_agent.config import Settings

from test_agent import GOOD, NOW, FakeBedrock, snapshot  # noqa: F401


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(ledger_path=tmp_path / "ledger.jsonl", monthly_budget_jpy=100.0)


def test_a_reservation_that_does_not_fit_is_refused_before_the_call(tmp_path: Path) -> None:
    tiny = Settings(ledger_path=tmp_path / "ledger.jsonl", monthly_budget_jpy=0.5)
    bedrock = FakeBedrock()
    with pytest.raises(BudgetExceeded):
        analyze(tiny, bedrock, snapshot(herta="outage"), BudgetLedger(tiny), now=NOW)
    assert bedrock.calls == []
    assert BudgetLedger(tiny).month_spent_jpy(NOW) == 0.0


def test_concurrent_runs_cannot_overspend_the_budget(tmp_path: Path) -> None:
    one = cost_jpy(Settings(), 1000, 700)
    settings = Settings(ledger_path=tmp_path / "ledger.jsonl", monthly_budget_jpy=one * 3.5)
    ledger = BudgetLedger(settings)
    results: list[str] = []

    def attempt() -> None:
        try:
            ledger.reserve(NOW, input_tokens=1000, output_tokens=700)
            results.append("ok")
        except BudgetExceeded:
            results.append("refused")

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert results.count("ok") == 3  # exactly what fits, never more
    assert ledger.month_spent_jpy(NOW) <= settings.monthly_budget_jpy


def test_the_reservation_uses_utf8_bytes_so_emoji_cannot_be_under_counted(settings: Settings) -> None:
    snap = snapshot(incidents=[{"title": "\U0001F525" * 100, "status": "investigating"}])
    ledger = BudgetLedger(settings)
    seen: dict = {}

    class Probe(FakeBedrock):
        def converse(self, **kwargs):
            seen["during"] = ledger.month_spent_jpy(NOW)
            return super().converse(**kwargs)

    analyze(settings, Probe(), snap, ledger, now=NOW)
    as_bytes = len(json.dumps(build_request(settings, snap), ensure_ascii=False).encode("utf-8"))
    as_chars = len(json.dumps(build_request(settings, snap), ensure_ascii=False))
    assert as_bytes > as_chars  # the case the finding is about
    assert seen["during"] == pytest.approx(cost_jpy(settings, as_bytes, settings.max_output_tokens), rel=1e-3)


@pytest.mark.parametrize("usage", [None, {}, {"inputTokens": 10}, {"outputTokens": 10}, {"inputTokens": "x", "outputTokens": 1}])
def test_missing_usage_keeps_the_reservation_instead_of_settling_to_zero(settings: Settings, usage) -> None:
    class NoUsage(FakeBedrock):
        def converse(self, **kwargs):
            response = super().converse(**kwargs)
            if usage is None:
                response.pop("usage")
            else:
                response["usage"] = usage
            return response

    ledger = BudgetLedger(settings)
    outcome = analyze(settings, NoUsage(), snapshot(herta="outage"), ledger, now=NOW)
    assert outcome.analysis is not None  # the analysis is still delivered
    reserved = [json.loads(line) for line in settings.ledger_path.read_text().splitlines()]
    assert [record["kind"] for record in reserved] == ["reservation"]  # no zero settlement offset it
    assert ledger.month_spent_jpy(NOW) == pytest.approx(outcome.cost_jpy, rel=1e-3) and outcome.cost_jpy > 0


def test_the_bedrock_client_is_built_without_sdk_retries(monkeypatch, tmp_path: Path) -> None:
    import boto3

    from ops_agent import __main__ as cli

    captured: dict = {}

    class FakeStatusClient:
        def __init__(self, *_args, **_kwargs) -> None: ...

        def snapshot(self):
            return snapshot(herta="outage")

    def fake_client(*_args, **kwargs):
        captured.update(kwargs)
        return FakeBedrock()

    monkeypatch.setattr(cli, "StatusClient", FakeStatusClient)
    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setenv("OPS_AGENT_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    assert cli.main([]) == 0
    retries = captured["config"].retries
    assert retries["max_attempts"] == 0  # a retry after a read timeout could be billed again
