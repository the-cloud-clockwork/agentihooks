from datetime import datetime, timezone

import pytest

from hooks.context import context_recycle as recycle

pytestmark = pytest.mark.unit


@pytest.fixture
def context(monkeypatch, tmp_path):
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(recycle, "_used", lambda _: 600_000)
    return {"AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}


def test_preparation_starts_at_the_limit_with_deadline(context):
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    directive = recycle.directive("preparing", context, now=now)
    assert "HANDOFF PREPARATION" in directive
    assert "write your handoff now" in directive.lower()
    assert "2026-10-06 12:25 UTC" in directive
    assert "before the 650k hard gate" in directive
    assert recycle.gate("Bash", {"command": "git commit"}, "preparing", context) is None
    assert recycle.directive("preparing", context, now=now) is None


def test_no_preparation_below_the_limit(context, monkeypatch):
    monkeypatch.setattr(recycle, "_used", lambda _: 599_999)
    assert recycle.directive("early", context) is None


def test_hard_gate_sits_fifty_thousand_above_the_limit(context, monkeypatch):
    monkeypatch.setattr(recycle, "_used", lambda _: 649_999)
    assert "PREPARATION" in recycle.directive("growing", context)
    assert recycle.over_limit("growing", context) is None
    assert recycle.gate("Bash", {"command": "git commit"}, "growing", context) is None
    monkeypatch.setattr(recycle, "_used", lambda _: 650_000)
    assert "650k" in recycle.directive("growing", context)
    assert recycle.over_limit("growing", context) == "sw"
    assert "BLOCKED" in recycle.gate("Bash", {"command": "git commit"}, "growing", context)


def test_directive_and_gate_texts_name_the_tokens_and_the_hard_gate(context, monkeypatch):
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    assert recycle.directive("texts", context, now=now) == (
        "HANDOFF PREPARATION — this session holds 600k tokens. Write your handoff now. "
        "Deadline: 2026-10-06 12:25 UTC, or before the 650k hard gate, whichever comes first. "
        + recycle._INSTRUCTIONS.format(slug="sw")
    )
    monkeypatch.setattr(recycle, "_used", lambda _: 650_000)
    assert recycle.directive("texts", context) == recycle._DIRECTIVE.format(used=650, limit=650, slug="sw")
    assert recycle.directive("texts", context) is None
    assert recycle.gate("Bash", {"command": "git commit"}, "texts", context) == "BLOCKED: " + (
        recycle._DIRECTIVE + recycle._ALLOWED
    ).format(used=650, limit=650, slug="sw")


def test_the_margin_setting_moves_the_hard_gate(context, monkeypatch):
    monkeypatch.setattr(recycle, "HANDOFF_MARGIN", 100)
    monkeypatch.setattr(recycle, "_used", lambda _: 699_999)
    assert recycle.over_limit("wide", context) is None
    monkeypatch.setattr(recycle, "_used", lambda _: 700_000)
    assert recycle.over_limit("wide", context) == "sw"


def test_default_margin_is_fifty_thousand():
    import hooks.config as config

    assert config.HANDOFF_MARGIN == 50


def test_preparation_never_reaches_unbound_sessions(context):
    assert recycle.directive("other", {}) is None
