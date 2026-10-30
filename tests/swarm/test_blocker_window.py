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


def _now_ms(store):
    seconds, microseconds = store.redis.time()
    return seconds * 1000 + microseconds // 1000


def _sustain(store):
    now = _now_ms(store)
    blockers.record(store, SLUG, "cat", "boom", now - HOUR)
    blockers.record(store, SLUG, "cat", "boom", now)


def test_a_non_master_session_is_never_touched(store, master):
    assert blocker_window.pretool("Bash", {"command": "echo hi"}, "") == (None, None)
    assert blocker_window.pretool("Bash", {"command": "echo hi"}, "some-other-session") == (None, None)
    assert not master_coding.is_open(SID)


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


def test_the_notice_fires_at_most_once_a_minute(store, master):
    _sustain(store)
    notice, _ = blocker_window.pretool("Read", {}, SID)
    assert notice is not None and "cat" in notice
    notice, _ = blocker_window.pretool("Read", {}, SID)
    assert notice is None


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


def test_a_different_swarm_merge_command_is_denied(store, master):
    _sustain(store)
    _, block = blocker_window.pretool("Bash", {"command": "agentihooks swarm other merge queue url"}, SID)
    assert block is not None


def test_a_store_failure_fails_open(master, monkeypatch):
    import redis

    def broken(environ=None):
        raise redis.RedisError("down")

    monkeypatch.setattr("scripts.swarm.store.redis_client", broken)
    assert blocker_window.pretool("Bash", {"command": "echo hi"}, SID) == (None, None)
