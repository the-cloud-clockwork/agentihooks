from unittest.mock import patch

from hooks.context.brain_adapter import BrainEntry
from hooks.context.project_cache import project_context, refresh_project_cache, store_feed
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_memory import ProjectMemory
from hooks.context.project_sessions import record_session


def test_shared_cache_refresh_and_feed_change(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    identity = ProjectIdentity("alpha", "org/alpha")
    record_session("one", identity)
    record_session("two", identity)
    entries = [BrainEntry("hot-arcs-test", "Active Hot Arcs", "first")]
    store_feed(entries)
    with patch("hooks._async.fork_and_call") as fork:
        assert "No project memory yet" in project_context("one")
        assert fork.call_count == 1
        args = fork.call_args.args
    with patch(
        "hooks.context.project_memory.VaultProjectSource.fetch",
        return_value=ProjectMemory("alpha", lessons=["alpha lesson"]),
    ) as fetch:
        refresh_project_cache(*args[1:])
        refresh_project_cache(*args[1:])
        assert fetch.call_count == 1
    with patch("hooks._async.fork_and_call") as fork:
        assert "alpha lesson" in project_context("one")
        assert "alpha lesson" in project_context("two")
        fork.assert_not_called()
        store_feed([BrainEntry("hot-arcs-test", "Active Hot Arcs", "second")])
        assert "alpha lesson" in project_context("one")
        assert fork.call_count == 1
    monkeypatch.setenv("BRAIN_PROJECT_SCOPE", "off")
    assert project_context("one") is None


def test_expired_cache_keeps_last_good_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    identity = ProjectIdentity("alpha", "alpha")
    record_session("one", identity)
    store_feed([])
    with patch("hooks._async.fork_and_call") as fork:
        project_context("one")
    args = fork.call_args.args[1:]
    with patch(
        "hooks.context.project_memory.VaultProjectSource.fetch", return_value=ProjectMemory("alpha", lessons=["kept"])
    ):
        refresh_project_cache(*args)
    monkeypatch.setenv("BRAIN_PROJECT_MEMORY_TTL", "0")
    with patch("hooks._async.fork_and_call") as fork:
        assert "kept" in project_context("one")
        assert fork.call_count == 1
    from hooks.context.brain_adapter import BrainSourceUnavailable

    with patch("hooks.context.project_memory.VaultProjectSource.fetch", side_effect=BrainSourceUnavailable("down")):
        refresh_project_cache(*args)
    with patch("hooks._async.fork_and_call"):
        assert "kept" in project_context("one")


def test_codex_refresh_defers_project_context_once(monkeypatch, tmp_path):
    from hooks.context.brain_adapter import maybe_refresh_on_tool_call
    from hooks.context.project_cache import take_project_context

    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr("hooks.config.BRAIN_ENABLED", True)
    monkeypatch.setattr("hooks.config.BRAIN_REFRESH_TOOL_CALLS", 1)
    with (
        patch("hooks.context.brain_adapter._refresh", return_value={"created_ids": []}),
        patch("hooks.context.project_cache.project_context", return_value="project proof"),
    ):
        assert maybe_refresh_on_tool_call("codex", 1, claim_delivery=False) is None
    assert take_project_context("codex") == "project proof"
    assert take_project_context("codex") is None
