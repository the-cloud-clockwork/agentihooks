import pytest

import hooks.context.context_recycle as recycle
from scripts.codex_context import CodexContext

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: None)
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: None)


def _swarm_env(name="sw-eng-1"):
    return {"AGENTIHOOKS_AGENT_NAME": name}


def test_default_limit_is_600_thousand():
    import hooks.config as config

    assert config.COMPACT_LIMIT == 600


def test_claude_agent_at_the_limit_gets_the_handoff_directive(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 600_000)
    text = recycle.directive("s1", _swarm_env("my-swarm-ci-2"))
    assert "agentihooks swarm my-swarm handoff" in text and "handoff document" in text


def test_codex_agent_over_the_limit_gets_the_directive(monkeypatch):
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: CodexContext(used=700_000, window=872_000))
    assert recycle.directive("s1", _swarm_env()) is not None


def test_below_the_limit_nothing(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 599_999)
    assert recycle.directive("s1", _swarm_env()) is None


def test_unknown_usage_nothing():
    assert recycle.directive("s1", _swarm_env()) is None


def test_non_swarm_session_nothing(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 900_000)
    assert recycle.directive("s1", {}) is None
    assert recycle.directive("s1", {"AGENTIHOOKS_AGENT_NAME": "my terminal"}) is None


def test_directive_is_given_once_per_session(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 900_000)
    assert recycle.directive("s1", _swarm_env()) is not None
    assert recycle.directive("s1", _swarm_env()) is None
    assert recycle.directive("s2", _swarm_env()) is not None
