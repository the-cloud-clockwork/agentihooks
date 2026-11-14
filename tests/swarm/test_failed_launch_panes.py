import json
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import herdr_panes
from scripts.herdr_host import Placement
from scripts.swarm import failed_panes, launch_check
from scripts.swarm import runtime as runtime_module
from scripts.swarm import tick as tick_module
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import SpawnError, tick
from tests.helpers import store_seed
from tests.swarm.test_tick import FakeRuntime, tasks

LAUNCH_CHECKED = True

SCREEN = "$ agentihooks codex --route default\nCodex account default not found\n\n$ \n\n"
FAILED_AT = 1_000
DUE = FAILED_AT + failed_panes.CLOSE_AFTER_MS


class PaneRuntime(FakeRuntime):
    def __init__(self, screens=None, **kwargs):
        super().__init__(**kwargs)
        self.screens, self.panes_closed, self.read = dict(screens or {}), [], []
        self.owned, self.close_error, self.list_error, self.asked = set(self.screens), None, None, []

    def pane_tail(self, pane_id, lines):
        self.read.append((pane_id, lines))
        if pane_id not in self.screens:
            raise RuntimeError(f"pane {pane_id} not found")
        return self.screens[pane_id]

    def launched_pane(self, pane_id, name):
        self.asked.append((pane_id, name))
        if self.list_error:
            raise RuntimeError(self.list_error)
        return pane_id in self.owned

    def close_pane(self, pane_id):
        if self.close_error:
            raise RuntimeError(self.close_error)
        self.panes_closed.append(pane_id)
        self.screens.pop(pane_id, None)


@pytest.fixture(autouse=True)
def unbundled(monkeypatch):
    from hooks.context import profile_chain

    monkeypatch.setattr(profile_chain, "read_state", lambda: {})
    monkeypatch.setattr(launch_check, "declared", lambda profile, worn=(): [])


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return s


def _failed_once(store):
    runtime = PaneRuntime({"w1:p9": SCREEN}, crash=SpawnError("Codex account default not found", pane_id="w1:p9"))
    ledger = tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT)
    runtime.crash = None
    store.update("sw", state="paused")
    return ledger, runtime


def _failed_row(store):
    return next(row for row in store.launches("sw") if row["state"] == "failed")


@pytest.mark.parametrize(
    ("agent", "live"),
    [
        (AgentRecord("master@a1b2c3-0001", "master", "", started_at=FAILED_AT, pane_id="w1:m1"), False),
        (AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w9:p1", runtime_backend="remote"), False),
        (AgentRecord("engineer@a1b2c3-0002", "eng", "t1", pane_id="w1:p2"), True),
    ],
)
def test_a_master_remote_or_live_launch_closes_its_pane_on_retirement(store, agent, live):
    runtime = PaneRuntime()
    if live:
        runtime.live.add(agent.name)
    assert tick_module._retire_launch("sw", store, runtime, agent, FAILED_AT) is True
    assert runtime.closed == [agent.pane_id] and failed_panes.held(store, "sw") == {}


@pytest.mark.feature("hive-launch-pane-off-controller-hold")
def test_a_hive_hosted_launch_keeps_its_pane_off_the_controller_hold(store, monkeypatch):
    monkeypatch.setenv("SWARM_HIVE_ID", "controller")
    runtime = PaneRuntime()
    agent = AgentRecord("engineer@a1b2c3-0004", "eng", "t1", pane_id="w5M:p1", runtime_target={"host_id": "root"})
    assert tick_module._retire_launch("sw", store, runtime, agent, FAILED_AT) is True
    assert runtime.closed == ["w5M:p1"] and failed_panes.held(store, "sw") == {}


def test_a_failed_retirement_holds_no_pane(store):
    runtime = PaneRuntime()
    agent = AgentRecord("engineer@a1b2c3-0003", "eng", "t1", pane_id="w1:p3")
    runtime.stuck.add(agent.name)
    assert tick_module._retire_launch("sw", store, runtime, agent, FAILED_AT) is False
    assert failed_panes.held(store, "sw") == {}


def test_lines_of_an_unknown_launch_are_not_recorded(store):
    store.note_launch_pane("sw", "nobody", ["x"], "why")
    assert store.launches("sw") == []


def test_lines_without_a_reason_keep_an_empty_error(store):
    store_seed.record_launch(store, "sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w1:p1"), "started")
    store.note_launch_pane("sw", "engineer@a1b2c3-0001", ["x"])
    assert [(r["error"], r["pane_lines"]) for r in store.launches("sw")] == [("", ["x"])]


def _launch_fails(tmp_path, stdout, returncode=0, run_error=None, stderr="Codex account default not found\n"):
    def run(argv, **kwargs):
        if "terminate-agent" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if run_error:
            raise run_error
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="full")
    with pytest.raises(SpawnError) as error:
        runtime.spawn(config, "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x"})
    return error.value


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("status=started\nroute_status=pending\npane_id=w1:p9\n", 0),
        ("status=started\nroute_status=refused\npane_id=w1:p9\n", 0),
        ("status=started\npane_id=w1:p9\n", 3),
    ],
)
def test_a_launch_that_never_reports_its_route_names_its_pane(tmp_path, stdout, returncode):
    error = _launch_fails(tmp_path, stdout, returncode)
    assert error.pane_id == "w1:p9"
    assert str(error) == "Codex account default not found"


def test_a_launch_whose_profile_validation_fails_names_its_pane(tmp_path):
    error = _launch_fails(tmp_path, "status=started\nroute_status=routed\npane_id=w1:p9\n")
    assert error.pane_id == "w1:p9"
    assert str(error) == "mounted profile validation is missing or failed"


def test_a_launch_that_opened_no_pane_names_none(tmp_path):
    assert _launch_fails(tmp_path, "", 2).pane_id == ""


def test_a_launch_whose_profile_validation_fails_without_a_pane_names_none(tmp_path):
    assert _launch_fails(tmp_path, "status=started\nroute_status=routed\n").pane_id == ""


def _record(monkeypatch, tmp_path, name, pane, terminal, at):
    environ = {herdr_panes.ROOT_ENV: str(tmp_path / "panes"), "HERDR_SOCKET_PATH": str(tmp_path / "herdr.sock")}
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    herdr_panes.record(Placement("w1", "w1:t1", pane, terminal), "init-agent", name, environ, at)


def test_a_timed_out_launch_names_the_newest_pane_it_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.time, "time", lambda: 5.0)
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0001", "w1:p6", "term_first", 5_000)
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0001", "w1:p7", "term_old", 5_001)
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0001", "w1:p9", "term_new", 5_002)
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0002", "w1:p8", "term_other", 5_003)
    error = _launch_fails(tmp_path, "", run_error=subprocess.TimeoutExpired("init-agent", 300))
    assert (error.pane_id, error.status, str(error)) == (
        "w1:p9",
        "ambiguous",
        "init-agent timed out for engineer@a1b2c3-0001",
    )


def test_a_timed_out_launch_never_adopts_a_pane_opened_before_it(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.time, "time", lambda: 5.0)
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0001", "w1:p7", "term_old", 4_999)
    error = _launch_fails(tmp_path, "", run_error=subprocess.TimeoutExpired("init-agent", 300))
    assert error.pane_id == ""


def test_a_timed_out_launch_adopts_a_pane_opened_the_moment_it_started(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.time, "time", lambda: 5.0)
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0001", "w1:p7", "term_now", 5_000)
    error = _launch_fails(tmp_path, "", run_error=subprocess.TimeoutExpired("init-agent", 300))
    assert error.pane_id == "w1:p7"


def test_a_pane_belongs_to_a_launch_only_while_herdr_shows_its_terminal(tmp_path, monkeypatch):
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0001", "w1:p9", "term_a", 1)
    listed = {"panes": [{"pane_id": "w1:p9", "terminal_id": "term_a"}]}

    def herdr(args):
        assert args == ["pane", "list"]
        return listed

    runtime = HerdrRuntime(home=tmp_path, herdr=herdr)
    assert runtime.launched_pane("w1:p9", "engineer@a1b2c3-0001") is True
    assert runtime.launched_pane("w1:p9", "engineer@a1b2c3-0002") is False
    listed["panes"] = [{"pane_id": "w1:p9", "terminal_id": "term_operator"}]
    assert runtime.launched_pane("w1:p9", "engineer@a1b2c3-0001") is False
    listed["panes"] = []
    assert runtime.launched_pane("w1:p9", "engineer@a1b2c3-0001") is False
    del listed["panes"]
    assert runtime.launched_pane("w1:p9", "engineer@a1b2c3-0001") is False


@pytest.mark.parametrize(
    "owner,name", [("engineer-a1b2c3-0001", "engineer@a1b2c3-0001"), ("engineer@a1b2c3-0001", "engineer-a1b2c3-0001")]
)
def test_a_failed_launch_recognizes_its_pane_under_either_owner_name(tmp_path, monkeypatch, owner, name):
    _record(monkeypatch, tmp_path, owner, "w1:p9", "term_a", 1)
    listed = {"panes": [{"pane_id": "w1:p9", "terminal_id": "term_a"}]}
    runtime = HerdrRuntime(home=tmp_path, herdr=lambda args: listed)
    assert runtime.launched_pane("w1:p9", name) is True
    assert runtime.launched_pane("w1:p9", "engineer@a1b2c3-0002") is False
    listed["panes"][0]["terminal_id"] = "term_operator"
    assert runtime.launched_pane("w1:p9", name) is False


def test_a_timed_out_claude_launch_recovers_its_plain_owner_pane(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module.time, "time", lambda: 5.0)
    _record(monkeypatch, tmp_path, "engineer-a1b2c3-0001", "w1:p6", "term_old", 4_999)
    _record(monkeypatch, tmp_path, "engineer-a1b2c3-0001", "w1:p7", "term_first", 5_000)
    _record(monkeypatch, tmp_path, "engineer-a1b2c3-0001", "w1:p9", "term_new", 5_001)
    _record(monkeypatch, tmp_path, "engineer-a1b2c3-0002", "w1:p8", "term_other", 5_002)
    error = _launch_fails(tmp_path, "", run_error=subprocess.TimeoutExpired("init-agent", 300))
    assert (error.pane_id, error.status, str(error)) == (
        "w1:p9",
        "ambiguous",
        "init-agent timed out for engineer@a1b2c3-0001",
    )


def test_the_runtime_reads_a_pane_tail_and_closes_it_by_its_id(tmp_path):
    calls = []

    def herdr(args):
        calls.append(args)
        return {"text": "one\ntwo\n"} if args[:2] == ["pane", "read"] else {}

    runtime = HerdrRuntime(home=tmp_path, herdr=herdr)
    assert runtime.pane_tail("w1:p9", 20) == "one\ntwo\n"
    runtime.close_pane("w1:p9")
    assert calls == [
        ["pane", "read", "w1:p9", "--source", "recent", "--lines", "20", "--format", "text"],
        ["pane", "close", "w1:p9"],
    ]


def test_closing_a_pane_herdr_no_longer_has_is_not_an_error(tmp_path):
    def herdr(args):
        raise RuntimeError("pane w1:p9 not found")

    HerdrRuntime(home=tmp_path, herdr=herdr).close_pane("w1:p9")


def test_closing_a_pane_that_herdr_refuses_raises(tmp_path):
    def herdr(args):
        raise RuntimeError("server down")

    with pytest.raises(RuntimeError, match="^server down$"):
        HerdrRuntime(home=tmp_path, herdr=herdr).close_pane("w1:p9")


def test_lines_drop_trailing_blanks_and_keep_the_last_ones():
    assert failed_panes.lines("a\nb  \n\nc\n\n\n", 2) == ["", "c"]
    assert failed_panes.lines("", 20) == []


class HistoryReads:
    """Wrap a Redis client and record each range read of a key."""

    def __init__(self, redis: Any) -> None:
        self.redis: Any = redis
        self.ranged: list[str] = []

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        self.ranged.append(key)
        return self.redis.lrange(key, start, end)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.redis, name)


@pytest.mark.feature("failed-panes-report-hash-only")
def test_the_failed_panes_report_reads_the_launches_hash_and_never_the_history_list(store: RedisStore) -> None:
    store.redis.hset(store.key("sw", "launches"), "e-1", json.dumps({"agent": "e-1", "task": "t1", "state": "started"}))
    failed = {"agent": "e-2", "task": "t2", "state": "failed", "error": "gone", "pane_id": "w1:p9"}
    store.redis.hset(store.key("sw", "launches"), "e-2", json.dumps(failed))
    store.note_launch_pane("sw", "e-2", ["$ codex", "account not found"])
    validated = {"validation": {"state": "validated"}}
    old = {"name": "e-0", "task": "t0", "started_at": 5, "profile_decision": validated}
    store.redis.rpush(store.key("sw", "history"), json.dumps(old))
    reads = HistoryReads(store.redis)
    store.redis = reads

    rows = failed_panes.report(store, "sw")

    assert rows == [{**failed, "pane_lines": ["$ codex", "account not found"]}]
    assert reads.ranged == []
