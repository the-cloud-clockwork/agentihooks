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


def test_current_unowned_folder_does_not_reuse_old_session_project(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    record_session("resumed", ProjectIdentity("alpha", "alpha"))
    with patch("hooks.context.project_cache.resolve_project", return_value=None):
        assert project_context("resumed", "") is None
    with patch("hooks.context.broadcast._load_sessions", return_value={"resumed": {"project": None}}):
        assert project_context("resumed") is None


def test_obsolete_worker_cannot_replace_new_feed_cache(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    record_session("ordered", ProjectIdentity("alpha", "alpha"))
    store_feed([BrainEntry("hot-arcs", "Arcs", "old")])
    with patch("hooks._async.fork_and_call") as fork:
        project_context("ordered")
        old_args = fork.call_args.args[1:]
    store_feed([BrainEntry("hot-arcs", "Arcs", "new")])
    with patch("hooks._async.fork_and_call") as fork:
        project_context("ordered")
        new_args = fork.call_args.args[1:]
    with patch(
        "hooks.context.project_memory.VaultProjectSource.fetch",
        side_effect=[ProjectMemory("alpha", lessons=["new"]), ProjectMemory("alpha", lessons=["old"])],
    ) as fetch:
        refresh_project_cache(*new_args)
        refresh_project_cache(*old_args)
    with patch("hooks._async.fork_and_call") as fork:
        assert "new" in project_context("ordered")
        fork.assert_not_called()
    assert fetch.call_count == 1


def test_swarm_overview_reads_the_ledger_overview_from_the_ledger_folder(monkeypatch, tmp_path):
    from hooks.context.project_cache import _swarm_overview
    from tests.swarm_ledger import legacy_page

    folder = tmp_path / "development-ledger"
    folder.mkdir()
    legacy_page.store(folder, "rig", {"title": "Rig", "overview": "Durable agent coordination."})
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    monkeypatch.setenv("LEDGER_DIR", str(folder))
    assert _swarm_overview() == "Durable agent coordination."
    monkeypatch.delenv("LEDGER_DIR")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    assert _swarm_overview() == "Durable agent coordination."
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "other")
    assert _swarm_overview() == ""
