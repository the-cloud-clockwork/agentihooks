import json
from pathlib import Path

import pytest

from hooks.context import blocker_window, master_coding
from scripts.swarm import blockers
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

SID = "master-session"
SLUG = "sw"
HOUR = blockers.SUSTAINED_MS


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    found = RedisStore(redis)
    found.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: redis)
    return found


@pytest.fixture
def master(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    master_coding.record_seat(SID, SLUG, "master@sw")
    return tmp_path


def _sustain(store):
    now = blocker_window._now_ms(store)
    blockers.record(store, SLUG, "cat", "boom", now - HOUR)
    blockers.record(store, SLUG, "cat", "boom", now)


def test_a_non_master_session_is_never_touched(store, master):
    assert blocker_window.pretool("Bash", {"command": "echo hi"}, "") == (None, None)
    assert blocker_window.pretool("Bash", {"command": "echo hi"}, "some-other-session") == (None, None)
    assert not master_coding.is_open(SID)


def test_pretool_requires_both_a_session_and_master_status(store, master, monkeypatch):
    monkeypatch.setattr(master_coding, "is_master", lambda session_id: False)
    _sustain(store)
    assert blocker_window.pretool("Read", {}, SID) == (None, None)


def test_the_window_opens_automatically_once_sustained(store, master):
    assert not master_coding.is_open(SID)
    _sustain(store)
    blocker_window.pretool("Read", {}, SID)
    assert master_coding.is_open(SID)
    assert master_coding.opened_for_blockers(SID)


def test_the_window_closes_once_nothing_sustains(store, master):
    _sustain(store)
    blocker_window.pretool("Read", {}, SID)
    assert master_coding.is_open(SID)
    blockers.clear(store, SLUG, "cat")
    blocker_window.pretool("Read", {}, SID)
    assert not master_coding.is_open(SID)


def test_an_operator_opened_window_is_never_auto_closed(store, master):
    master_coding.observe(SID, "allow-master-coding", typed=True)
    blocker_window.pretool("Read", {}, SID)
    assert master_coding.is_open(SID)
    assert not master_coding.opened_for_blockers(SID)


def test_an_operator_opened_window_stays_marked_as_the_operators_even_while_sustained(store, master):
    master_coding.observe(SID, "allow-master-coding", typed=True)
    _sustain(store)
    blocker_window.pretool("Read", {}, SID)
    assert master_coding.is_open(SID)
    assert not master_coding.opened_for_blockers(SID)


def test_the_notice_fires_at_most_once_a_minute(store, master):
    _sustain(store)
    notice, _ = blocker_window.pretool("Read", {}, SID)
    assert notice == blocker_window.NOTICE.format(slug=SLUG, categories="cat for 60 minutes")
    notice, _ = blocker_window.pretool("Read", {}, SID)
    assert notice is None


def test_describe_floors_the_minutes(store):
    blockers.record(store, SLUG, "cat", "d", 30_000)
    blockers.record(store, SLUG, "cat", "d", 120_000)
    found = blockers.active(store, SLUG, 120_000)
    assert blocker_window._describe(found) == "cat for 1 minutes"


def test_describe_uses_60000_ms_per_minute(store):
    blockers.record(store, SLUG, "cat", "d", 0)
    blockers.record(store, SLUG, "cat", "d", 60_000)
    found = blockers.active(store, SLUG, 60_000)
    assert blocker_window._describe(found) == "cat for 1 minutes"


def test_describe_joins_multiple_categories_with_a_comma(store):
    blockers.record(store, SLUG, "alpha", "d", 0)
    blockers.record(store, SLUG, "beta", "d", 0)
    found = blockers.active(store, SLUG, 0)
    assert blocker_window._describe(found) == "alpha for 0 minutes, beta for 0 minutes"


def test_in_worktree_resolves_relative_to_dot_when_cwd_is_empty(monkeypatch):
    monkeypatch.setattr(blocker_window, "_WORKTREES", Path.cwd())
    assert blocker_window._in_worktree(".", "") is True


def test_slug_defaults_to_empty_string(monkeypatch):
    monkeypatch.setattr(master_coding, "seat", lambda session_id: {})
    assert blocker_window._slug(SID) == ""


def test_now_ms_combines_seconds_and_microseconds():
    class _Redis:
        def time(self):
            return (1000, 500000)

    class _Store:
        redis = _Redis()

    assert blocker_window._now_ms(_Store()) == 1_000_500


def test_the_window_records_now_ms_divided_by_1000(store, master, monkeypatch):
    monkeypatch.setattr(blocker_window, "_now_ms", lambda store: 2_000_000)
    _sustain(store)
    blocker_window.pretool("Read", {}, SID)
    recorded = json.loads(master_coding.path(SID).read_text())["at"]
    assert recorded == 2000.0


def test_a_notebook_edit_without_a_file_path_uses_the_notebook_path(store, master):
    _sustain(store)
    target = blocker_window._WORKTREES / "agentihooks" / "mine" / "nb.ipynb"
    _, block = blocker_window.pretool("NotebookEdit", {"notebook_path": str(target)}, SID)
    assert block is None


def test_an_unlisted_tool_is_denied(store, master):
    _sustain(store)
    _, block = blocker_window.pretool("WebFetch", {}, SID)
    assert block is not None


def test_allowed_bash_receives_the_literal_command_default(store, master, monkeypatch):
    captured = {}
    monkeypatch.setattr(blocker_window, "_allowed_bash", lambda command, slug: captured.setdefault("command", command))
    _sustain(store)
    blocker_window.pretool("Bash", {}, SID)
    assert captured["command"] == ""


def test_reads_and_the_allow_listed_commands_pass(store, master):
    _sustain(store)
    for tool, tool_input in (
        ("Read", {"file_path": "x"}),
        ("Bash", {"command": "git status"}),
        ("Bash", {"command": "gh pr view 1"}),
        ("Bash", {"command": f"agentihooks swarm {SLUG} blockers"}),
        ("Bash", {"command": f"agentihooks swarm {SLUG} merge queue url"}),
        ("Bash", {"command": "agentihooks msg inbox"}),
        ("Bash", {"command": f"agentihooks ledger --slug {SLUG} comment t1 hi"}),
    ):
        _, block = blocker_window.pretool(tool, tool_input, SID)
        assert block is None, f"{tool} {tool_input} was blocked"


def test_edits_inside_the_worktree_pass(store, master):
    _sustain(store)
    target = blocker_window._WORKTREES / "agentihooks" / "mine" / "f.py"
    _, block = blocker_window.pretool("Edit", {"file_path": str(target)}, SID)
    assert block is None


def test_a_tool_call_outside_the_fix_is_denied(store, master):
    _sustain(store)
    _, block = blocker_window.pretool("Bash", {"command": "echo hi"}, SID)
    assert block is not None and "cat" in block
    _, block = blocker_window.pretool("Write", {"file_path": "/etc/hosts"}, SID)
    assert block is not None


def test_the_block_names_the_swarm(store, master):
    _sustain(store)
    _, block = blocker_window.pretool("Bash", {"command": "echo hi"}, SID)
    assert block == blocker_window.BLOCK.format(slug=SLUG, categories="cat for 60 minutes")


def test_a_different_swarm_merge_command_is_denied(store, master):
    _sustain(store)
    _, block = blocker_window.pretool("Bash", {"command": "agentihooks swarm other merge queue url"}, SID)
    assert block is not None


def test_bash_agentihooks_alone_is_denied(store, master):
    _sustain(store)
    _, block = blocker_window.pretool("Bash", {"command": "agentihooks"}, SID)
    assert block is not None


def test_allowed_tokens_accepts_an_exact_two_token_command():
    assert blocker_window._allowed_tokens(["agentihooks", "msg"], SLUG) is True


def test_allowed_tokens_denies_an_empty_token_list():
    assert blocker_window._allowed_tokens([], SLUG) is False


def test_serena_tool_calls_pass(store, master):
    _sustain(store)
    _, block = blocker_window.pretool("mcp__serena__find_symbol", {}, SID)
    assert block is None


def test_write_tool_without_a_path_is_denied_before_checking_the_worktree(store, master, monkeypatch):
    called = []
    monkeypatch.setattr(blocker_window, "_in_worktree", lambda value, cwd: called.append(value) or True)
    _sustain(store)
    _, block = blocker_window.pretool("Edit", {}, SID)
    assert block is not None
    assert called == []


def test_pretool_default_cwd_argument(store, master, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        blocker_window, "_allowed", lambda tool_name, tool_input, cwd, slug: captured.setdefault("cwd", cwd)
    )
    _sustain(store)
    blocker_window.pretool("Edit", {"file_path": "f.py"}, SID)
    assert captured["cwd"] == ""


def test_a_store_failure_fails_open(master, monkeypatch):
    import redis

    def broken(environ=None):
        raise redis.RedisError("down")

    monkeypatch.setattr("scripts.swarm.store.redis_client", broken)
    assert blocker_window.pretool("Bash", {"command": "echo hi"}, SID) == (None, None)


def test_checked_denies_and_notes_the_notice_kwargs(store, master, monkeypatch):
    captured = {}
    monkeypatch.setattr("hooks.common.inject_context", lambda *args, **kwargs: captured.update(kwargs))
    _sustain(store)
    block = blocker_window.checked({"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "session_id": SID})
    assert block is not None and "cat" in block
    assert captured == {"also_log": False, "skip_compression": True, "kind": "alert"}


def test_checked_resolves_a_relative_edit_against_the_given_cwd(store, master, monkeypatch):
    monkeypatch.setattr("hooks.common.inject_context", lambda *args, **kwargs: None)
    _sustain(store)
    workdir = blocker_window._WORKTREES / "agentihooks" / "mine"
    block = blocker_window.checked(
        {"tool_name": "Edit", "tool_input": {"file_path": "f.py"}, "session_id": SID, "cwd": str(workdir)}
    )
    assert block is None


def test_open_for_blockers_mkdir_is_idempotent(master):
    master_coding.open_for_blockers(SID)
    master_coding.open_for_blockers(SID)


def test_open_for_blockers_writes_at_and_reason(master):
    master_coding.open_for_blockers(SID, now=42.0)
    assert json.loads(master_coding.path(SID).read_text()) == {"at": 42.0, "reason": "blockers"}


def test_opened_for_blockers_is_false_when_unset(master):
    assert master_coding.opened_for_blockers(SID) is False


def test_checked_calls_pretool_with_the_payloads_fields(monkeypatch):
    captured = {}

    def fake_pretool(tool_name, tool_input, session_id, cwd):
        captured.update(tool_name=tool_name, tool_input=tool_input, session_id=session_id, cwd=cwd)
        return None, None

    monkeypatch.setattr(blocker_window, "pretool", fake_pretool)
    blocker_window.checked({"tool_name": "Edit", "tool_input": {"file_path": "x"}, "session_id": SID, "cwd": "/work"})
    assert captured == {"tool_name": "Edit", "tool_input": {"file_path": "x"}, "session_id": SID, "cwd": "/work"}


def test_checked_defaults_missing_payload_fields(monkeypatch):
    captured = {}

    def fake_pretool(tool_name, tool_input, session_id, cwd):
        captured.update(tool_name=tool_name, tool_input=tool_input, session_id=session_id, cwd=cwd)
        return None, None

    monkeypatch.setattr(blocker_window, "pretool", fake_pretool)
    blocker_window.checked({})
    assert captured == {"tool_name": "", "tool_input": {}, "session_id": "", "cwd": ""}
