import subprocess
from types import SimpleNamespace

import pytest

from scripts import herdr_panes
from scripts.herdr_host import Placement
from scripts.swarm import failed_panes, launch_check
from scripts.swarm import runtime as runtime_module
from scripts.swarm import tick as tick_module
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import EXITED_BEFORE_LIFE, SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, tasks

pytestmark = pytest.mark.xdist_group("fakeredis")
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


def test_a_failed_launch_records_its_pane_and_keeps_it_open_for_two_minutes(store):
    ledger, runtime = _failed_once(store)
    tick("sw", store, ledger, runtime, now_ms=DUE - 1)
    assert failed_panes.CLOSE_AFTER_MS == 120_000
    assert _failed_row(store)["pane_id"] == "w1:p9"
    assert runtime.panes_closed == [] and runtime.read == []
    assert failed_panes.held(store, "sw") == {
        "w1:p9": {"agent": _failed_row(store)["agent"], "task": "t1", "failed_at": FAILED_AT, "reason": ""}
    }


def test_a_failed_launch_pane_closes_after_two_minutes_with_its_last_lines_kept(store):
    ledger, runtime = _failed_once(store)
    actions = tick("sw", store, ledger, runtime, now_ms=DUE)
    assert runtime.read == [("w1:p9", failed_panes.TAIL_LINES)]
    assert failed_panes.TAIL_LINES == 20
    assert runtime.panes_closed == ["w1:p9"]
    row = _failed_row(store)
    assert row["pane_lines"] == ["$ agentihooks codex --route default", "Codex account default not found", "", "$"]
    assert row["error"] == "Codex account default not found"
    assert runtime.asked == [("w1:p9", row["agent"])]
    assert failed_panes.held(store, "sw") == {}
    assert f"closed pane w1:p9 of the failed launch of {row['agent']} for t1" in actions
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT + 3 * failed_panes.CLOSE_AFTER_MS)
    assert runtime.panes_closed == ["w1:p9"]


def test_a_pane_whose_lines_cannot_be_read_still_closes_and_says_so(store):
    _, runtime = _failed_once(store)
    runtime.screens.clear()
    failed_panes.sweep("sw", store, runtime, DUE)
    assert runtime.panes_closed == ["w1:p9"]
    assert _failed_row(store)["pane_lines"] == ["its lines could not be read: pane w1:p9 not found"]


def test_a_pane_that_no_longer_shows_the_launch_is_dropped_untouched(store):
    _, runtime = _failed_once(store)
    runtime.owned.clear()
    actions = failed_panes.sweep("sw", store, runtime, DUE)
    agent = _failed_row(store)["agent"]
    assert actions == [f"dropped pane w1:p9: herdr no longer shows the failed launch of {agent} there"]
    assert runtime.panes_closed == [] and runtime.read == [] and failed_panes.held(store, "sw") == {}
    assert "pane_lines" not in _failed_row(store)


def test_a_pane_herdr_refuses_to_close_stays_held_for_the_next_tick(store):
    _, runtime = _failed_once(store)
    runtime.close_error = "server down"
    agent = _failed_row(store)["agent"]
    actions = failed_panes.sweep("sw", store, runtime, DUE)
    assert actions == [f"could not close pane w1:p9 of the failed launch of {agent}, retrying next tick: server down"]
    assert list(failed_panes.held(store, "sw")) == ["w1:p9"]
    runtime.close_error = None
    failed_panes.sweep("sw", store, runtime, DUE + 60_000)
    assert runtime.panes_closed == ["w1:p9"] and failed_panes.held(store, "sw") == {}


def test_a_pane_herdr_cannot_list_stays_held_unread_for_the_next_tick(store):
    _, runtime = _failed_once(store)
    runtime.list_error = "server down"
    agent = _failed_row(store)["agent"]
    actions = failed_panes.sweep("sw", store, runtime, DUE)
    assert actions == [f"could not read pane w1:p9 of the failed launch of {agent}, retrying next tick: server down"]
    assert list(failed_panes.held(store, "sw")) == ["w1:p9"]
    assert runtime.read == [] and runtime.panes_closed == [] and "pane_lines" not in _failed_row(store)


def test_a_live_agent_pane_is_never_read_or_closed(store):
    _, runtime = _failed_once(store)
    live = AgentRecord("engineer@a1b2c3-0009", "eng", "t2", started_at=FAILED_AT, pane_id="w1:p9", state="working")
    store.put_agent("sw", live)
    runtime.live.add(live.name)
    actions = failed_panes.sweep("sw", store, runtime, DUE)
    assert runtime.panes_closed == [] and runtime.read == []
    assert failed_panes.held(store, "sw") == {}
    assert actions == ["kept pane w1:p9: engineer@a1b2c3-0009 holds it now"]


def test_a_failed_launch_whose_process_survived_closes_only_once_it_is_gone(store):
    _, runtime = _failed_once(store)
    agent = _failed_row(store)["agent"]
    runtime.live.add(agent)
    assert failed_panes.sweep("sw", store, runtime, DUE) == []
    assert runtime.panes_closed == [] and list(failed_panes.held(store, "sw")) == ["w1:p9"]
    runtime.live.discard(agent)
    failed_panes.sweep("sw", store, runtime, DUE + 60_000)
    assert runtime.panes_closed == ["w1:p9"]


def test_the_tick_reaps_a_surviving_failed_launch_and_then_closes_its_pane(store):
    ledger, runtime = _failed_once(store)
    agent = _failed_row(store)["agent"]
    runtime.live.add(agent)
    actions = tick("sw", store, ledger, runtime, now_ms=DUE)
    assert runtime.reaped == [agent]
    assert actions.index(f"reaped stray {agent}") < actions.index(
        f"closed pane w1:p9 of the failed launch of {agent} for t1"
    )
    assert runtime.panes_closed == ["w1:p9"]


def test_a_spawn_failure_without_a_pane_holds_nothing(store):
    runtime = PaneRuntime(crash=SpawnError("init-agent exit 2"))
    tick("sw", store, tasks(("t1", "eng")), runtime, now_ms=FAILED_AT)
    assert failed_panes.held(store, "sw") == {}
    assert _failed_row(store)["pane_id"] == ""


def test_a_crash_that_is_no_spawn_error_records_no_pane(store):
    runtime = PaneRuntime(crash=OSError("worktree timer"))
    tick("sw", store, tasks(("t1", "eng")), runtime, now_ms=FAILED_AT)
    assert failed_panes.held(store, "sw") == {}
    assert _failed_row(store)["pane_id"] == ""


def test_a_launch_waiting_on_quota_holds_no_pane(store):
    runtime = PaneRuntime(crash=SpawnError("no seats", "unavailable", pane_id="w1:p9"))
    tick("sw", store, tasks(("t1", "eng")), runtime, now_ms=FAILED_AT)
    assert failed_panes.held(store, "sw") == {}


def test_a_started_launch_records_its_pane(store):
    runtime = PaneRuntime()
    tick("sw", store, tasks(("t1", "eng")), runtime, now_ms=FAILED_AT)
    started = [row for row in store.launches("sw") if row["state"] == "started"]
    assert [row["pane_id"] for row in started] == ["w1:p1"]


def test_a_worker_that_exits_before_its_launch_check_passes_has_its_pane_held_then_closed(store):
    ledger, runtime = tasks(("t1", "eng")), PaneRuntime({"w1:p1": "Error: model not available\n$ "})
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT)
    agent = next(a for a in store.agents("sw") if a.task == "t1")
    runtime.live.discard(agent.name)
    store.update("sw", state="paused")
    failed = FAILED_AT + launch_check.DEADLINE_MS
    tick("sw", store, ledger, runtime, now_ms=failed)
    assert "w1:p1" not in runtime.closed and runtime.closed.count("") == 1
    assert failed_panes.held(store, "sw")["w1:p1"]["reason"] == EXITED_BEFORE_LIFE
    tick("sw", store, ledger, runtime, now_ms=failed + failed_panes.CLOSE_AFTER_MS)
    assert runtime.panes_closed == ["w1:p1"]
    row = next(r for r in store.launches("sw") if r["agent"] == agent.name)
    assert row["pane_lines"] == ["Error: model not available", "$"]
    assert failed_panes.status_lines(store, "sw") == [
        f"failed launch  {agent.name}  t1  pane w1:p1 closed: {EXITED_BEFORE_LIFE}",
        "  | Error: model not available",
        "  | $",
    ]


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
    store.record_launch("sw", AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w1:p1"), "started")
    store.note_launch_pane("sw", "engineer@a1b2c3-0001", ["x"])
    assert [(r["error"], r["pane_lines"]) for r in store.launches("sw")] == [("", ["x"])]


def test_status_lists_failed_launches_with_their_lines(store):
    ledger, runtime = _failed_once(store)
    tick("sw", store, ledger, runtime, now_ms=DUE)
    rows = failed_panes.report(store, "sw")
    assert [(r["task"], r["pane_id"], r["pane_lines"][-1]) for r in rows] == [("t1", "w1:p9", "$")]


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


@pytest.mark.parametrize("stdout", ["", "status=started\nroute_status=pending\n"])
def test_a_route_timeout_without_a_pane_report_closes_its_recorded_pane_after_two_minutes(
    store, tmp_path, monkeypatch, stdout
):
    monkeypatch.setattr(runtime_module.time, "time", lambda: 5.0)
    _record(monkeypatch, tmp_path, "engineer@a1b2c3-0001", "w1:p9", "term_route", 5_000)
    error = _launch_fails(tmp_path, stdout, returncode=3, stderr="the harness did not report its route within 90s\n")
    assert str(error) == "the harness did not report its route within 90s"
    runtime = PaneRuntime({"w1:p9": SCREEN}, crash=error)
    ledger = tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT)
    assert _failed_row(store)["pane_id"] == "w1:p9"
    runtime.crash = None
    store.update("sw", state="paused")
    tick("sw", store, ledger, runtime, now_ms=DUE - 1)
    assert runtime.panes_closed == []
    tick("sw", store, ledger, runtime, now_ms=DUE)
    assert runtime.panes_closed == ["w1:p9"]
    assert failed_panes.held(store, "sw") == {}


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
