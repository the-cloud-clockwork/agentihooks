from types import SimpleNamespace

import pytest

from scripts.swarm import failed_panes, launch_check
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, tasks

pytestmark = pytest.mark.xdist_group("fakeredis")

SCREEN = "$ agentihooks codex --route default\nCodex account default not found\n\n$ \n\n"
FAILED_AT = 1_000


class PaneRuntime(FakeRuntime):
    def __init__(self, screens=None, **kwargs):
        super().__init__(**kwargs)
        self.screens, self.panes_closed, self.read = dict(screens or {}), [], []

    def pane_tail(self, pane_id, lines):
        self.read.append((pane_id, lines))
        if pane_id not in self.screens:
            raise RuntimeError(f"pane {pane_id} not found")
        return self.screens[pane_id]

    def close_pane(self, pane_id):
        self.panes_closed.append(pane_id)
        self.screens.pop(pane_id, None)


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
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT + failed_panes.CLOSE_AFTER_MS - 1)
    assert failed_panes.CLOSE_AFTER_MS == 120_000
    assert _failed_row(store)["pane_id"] == "w1:p9"
    assert runtime.panes_closed == [] and runtime.read == []
    assert list(failed_panes.held(store, "sw")) == ["w1:p9"]


def test_a_failed_launch_pane_closes_after_two_minutes_with_its_last_lines_kept(store):
    ledger, runtime = _failed_once(store)
    actions = tick("sw", store, ledger, runtime, now_ms=FAILED_AT + failed_panes.CLOSE_AFTER_MS)
    assert runtime.read == [("w1:p9", failed_panes.TAIL_LINES)]
    assert failed_panes.TAIL_LINES == 20
    assert runtime.panes_closed == ["w1:p9"]
    row = _failed_row(store)
    assert row["pane_lines"] == ["$ agentihooks codex --route default", "Codex account default not found", "", "$"]
    assert row["pane_closed_at"] == FAILED_AT + failed_panes.CLOSE_AFTER_MS
    assert failed_panes.held(store, "sw") == {}
    assert f"closed pane w1:p9 of the failed launch of {row['agent']} for t1" in actions
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT + 3 * failed_panes.CLOSE_AFTER_MS)
    assert runtime.panes_closed == ["w1:p9"]


def test_a_pane_already_gone_still_closes_and_says_its_lines_were_lost(store):
    ledger, runtime = _failed_once(store)
    runtime.screens.clear()
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT + failed_panes.CLOSE_AFTER_MS)
    assert runtime.panes_closed == ["w1:p9"]
    assert _failed_row(store)["pane_lines"] == ["its lines could not be read: pane w1:p9 not found"]


def test_a_live_agent_pane_is_never_read_or_closed(store):
    _, runtime = _failed_once(store)
    live = AgentRecord("engineer@a1b2c3-0009", "eng", "t2", started_at=FAILED_AT, pane_id="w1:p9", state="working")
    store.put_agent("sw", live)
    runtime.live.add(live.name)
    actions = failed_panes.sweep("sw", store, runtime, FAILED_AT + failed_panes.CLOSE_AFTER_MS)
    assert runtime.panes_closed == [] and runtime.read == []
    assert failed_panes.held(store, "sw") == {}
    assert "kept pane w1:p9: engineer@a1b2c3-0009 holds it now" in actions


def test_a_failed_launch_whose_agent_came_up_keeps_its_pane(store):
    _, runtime = _failed_once(store)
    runtime.live.add(_failed_row(store)["agent"])
    actions = failed_panes.sweep("sw", store, runtime, FAILED_AT + failed_panes.CLOSE_AFTER_MS)
    assert runtime.panes_closed == [] and failed_panes.held(store, "sw") == {}
    assert actions == [f"kept pane w1:p9: {_failed_row(store)['agent']} is live"]


def test_a_spawn_failure_without_a_pane_holds_nothing(store):
    runtime = PaneRuntime(crash=SpawnError("init-agent exit 2"))
    tick("sw", store, tasks(("t1", "eng")), runtime, now_ms=FAILED_AT)
    assert failed_panes.held(store, "sw") == {}
    assert _failed_row(store)["pane_id"] == ""


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
    assert "w1:p1" not in runtime.closed
    assert failed_panes.held(store, "sw")["w1:p1"]["agent"] == agent.name
    tick("sw", store, ledger, runtime, now_ms=failed + failed_panes.CLOSE_AFTER_MS)
    assert runtime.panes_closed == ["w1:p1"]
    row = next(r for r in store.launches("sw") if r["agent"] == agent.name)
    assert row["pane_lines"] == ["Error: model not available", "$"]


def test_status_lists_failed_launches_with_their_lines(store):
    ledger, runtime = _failed_once(store)
    tick("sw", store, ledger, runtime, now_ms=FAILED_AT + failed_panes.CLOSE_AFTER_MS)
    rows = failed_panes.report(store, "sw")
    assert [(r["task"], r["pane_id"], r["pane_lines"][-1]) for r in rows] == [("t1", "w1:p9", "$")]


def _launch_fails(tmp_path, stdout, returncode=0):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "terminate-agent" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="Codex account default not found\n")

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


def test_a_launch_that_opened_no_pane_names_none(tmp_path):
    assert _launch_fails(tmp_path, "", 2).pane_id == ""


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
