from datetime import datetime, timezone

import pytest

from hooks.context import context_recycle as recycle

pytestmark = pytest.mark.unit


@pytest.fixture
def context(monkeypatch, tmp_path):
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "_used", lambda _: 480_000)
    return {"AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}


def test_preparation_starts_at_eighty_percent_with_deadline(context):
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    directive = recycle.directive("preparing", context, now=now)
    assert "HANDOFF PREPARATION" in directive
    assert "write your handoff now" in directive.lower()
    assert "2026-10-06 12:25 UTC" in directive
    assert "600k" in directive
    assert recycle.gate("Bash", {"command": "git commit"}, "preparing", context) is None
    assert recycle.directive("preparing", context, now=now) is None


def test_no_preparation_below_eighty_percent(context, monkeypatch):
    monkeypatch.setattr(recycle, "_used", lambda _: 479_999)
    assert recycle.directive("early", context) is None


def test_preparation_does_not_consume_the_hard_gate_directive(context, monkeypatch):
    assert "PREPARATION" in recycle.directive("growing", context)
    monkeypatch.setattr(recycle, "_used", lambda _: 600_000)
    assert "CONTEXT RECYCLE" in recycle.directive("growing", context)
    assert "BLOCKED" in recycle.gate("Bash", {"command": "git commit"}, "growing", context)


def test_preparation_never_reaches_unbound_sessions(context):
    assert recycle.directive("other", {}) is None
