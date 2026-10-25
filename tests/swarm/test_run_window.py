import json
from dataclasses import replace

import pytest

from scripts.gates import log as gate_log
from scripts.swarm import cli, run_window, tick
from scripts.swarm.store import AgentRecord, SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.unit

SLUG = "sw"
OWNER = "engineer@abc-0001"
MINUTE = 60_000
START = 1_792_886_400_000
END = START + 720 * MINUTE
WINDOW = {"start_ms": START, "minutes": 720}
ALERT_AT = END - 30 * MINUTE


class Ledger:
    def __init__(self):
        self.notices = []

    def notify(self, slug, text):
        assert slug == SLUG
        self.notices.append(text)


def _doc():
    return {
        "tasks": [
            {"id": "t1", "title": "Fix the claim", "state": "claimed", "claimed_by": OWNER},
            {"id": "t2", "title": "Done work", "state": "done", "claimed_by": ""},
            {"id": "t3", "title": "Waiting", "state": "open", "claimed_by": ""},
        ]
    }


@pytest.fixture
def swarm():
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(replace(SwarmConfig(SLUG, "/repo", 1, 1), window=dict(WINDOW)))
    store.put_agent(SLUG, AgentRecord(OWNER, "eng", "t1", seat="eng-1@sw", started_at=START))
    return store, Ledger()


def _step(store, ledger, at, gates=None):
    if gates is not None:
        store.update(SLUG, gates=gates)
    return run_window.step(SLUG, store.config(SLUG), store, ledger, _doc(), at)


def _items(store, address):
    from scripts.inbox.store import InboxStore

    return [item.text for item in InboxStore(store.redis).inbox(address)]


def test_the_window_round_trips_through_the_swarm_config(swarm):
    store, _ = swarm
    assert SwarmConfig(SLUG, "/repo", 1, 1).window == {}
    assert store.config(SLUG).window == WINDOW
    assert json.loads(store.redis.hget(store.key(SLUG, "config"), "window")) == WINDOW


def test_set_records_the_window_and_reports_elapsed_and_remaining(env, capsys, monkeypatch):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    monkeypatch.setattr(cli, "now_ms", lambda: START + 90 * MINUTE)
    capsys.readouterr()
    assert run("sw", "set", "window-start=2026-10-25T00:00:00Z", "window-minutes=720") == 0
    assert store.config("sw").window == WINDOW
    assert json.loads(capsys.readouterr().out)["window"] == {
        "start": "2026-10-25T00:00:00Z",
        "end": "2026-10-25T12:00:00Z",
        "elapsed_minutes": 90,
        "remaining_minutes": 630,
    }
    assert cli.setting(store.config("sw"), "window-start") == "2026-10-25T00:00:00Z"
    assert cli.setting(store.config("sw"), "window-minutes") == "720"


def test_set_starts_the_window_now_and_off_clears_it(env, capsys, monkeypatch):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    monkeypatch.setattr(cli, "now_ms", lambda: START)
    assert run("sw", "set", "window-start=now", "window-minutes=5") == 0
    assert store.config("sw").window == {"start_ms": START, "minutes": 5}
    capsys.readouterr()
    assert run("sw", "set", "window-start=off") == 0
    assert store.config("sw").window == {}
    assert json.loads(capsys.readouterr().out)["window"] == {}
    assert cli.setting(store.config("sw"), "window-start") == "unset"
    assert cli.setting(store.config("sw"), "window-minutes") == "unset"


@pytest.mark.parametrize(
    ("pair", "error"),
    [
        ("window-minutes=0", "window-minutes takes a whole number of minutes above 0"),
        ("window-minutes=ten", "window-minutes takes a whole number of minutes above 0"),
        ("window-start=tomorrow", "window-start takes now, off or a UTC time such as 2026-10-25T00:00:00Z"),
    ],
)
def test_set_refuses_a_bad_window_without_changing_config(env, capsys, pair, error):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    before = store.redis.hgetall(store.key("sw", "config"))
    capsys.readouterr()
    assert run("sw", "set", pair) == 1
    assert capsys.readouterr().err.strip() == f"swarm: {error}"
    assert store.redis.hgetall(store.key("sw", "config")) == before


def test_a_window_needs_both_its_start_and_its_length():
    assert run_window.span({}) is None
    assert run_window.span({"minutes": 5}) is None
    assert run_window.span({"start_ms": START}) is None
    assert run_window.span(WINDOW) == (START, END)
    assert run_window.report({"minutes": 5}, START) == {}


def test_the_window_gate_logs_only_by_default():
    from scripts.gates import catalog

    assert (run_window.GATE.name, run_window.GATE.default_mode) == ("window", "observe")
    assert catalog.defaults()["window"] == "observe"
    assert run_window.ALERT_MINUTES == 30


def test_the_tick_records_elapsed_and_remaining_before_the_alert(swarm):
    store, ledger = swarm
    assert _step(store, ledger, START + 60 * MINUTE) == []
    marks = store.redis.hgetall(store.key(SLUG, "run-window"))
    assert (marks["elapsed_minutes"], marks["remaining_minutes"]) == ("60", "660")
    assert _items(store, "master@sw") == [] and ledger.notices == []


def test_a_window_not_yet_started_is_left_alone(swarm):
    store, ledger = swarm
    assert _step(store, ledger, START - MINUTE) == []
    assert store.redis.hgetall(store.key(SLUG, "run-window")) == {}


def test_the_master_hears_once_before_the_cutoff(swarm):
    store, ledger = swarm
    assert _step(store, ledger, ALERT_AT) == ["run window: told master@sw it ends at 25 October 12:00 UTC"]
    assert _step(store, ledger, ALERT_AT + MINUTE) == []
    assert _items(store, "master@sw") == [
        "The run window on swarm sw ends at 25 October 12:00 UTC, in 30 minutes. At the cutoff the window gate, set "
        "to log only, logs the claims it would stop and the hand offs it would ask, and the swarm publishes the final "
        "state to the ledger and the watchdog. Only swarm set changes the window. Close this item once you have looked."
    ]


FINAL_OBSERVE = (
    "## Intent\nRun window on swarm sw, 25 October 00:00 UTC to 25 October 12:00 UTC, window gate log only.\n\n"
    "## Done\nTasks by state: 1 claimed, 1 done, 1 open.\n\n"
    "## Stopped at\nIn flight at the cutoff: engineer@abc-0001 on t1 Fix the claim.\n\n"
    "## Decisions and promises\nOnly swarm set changes the window.\n\n"
    "## Next\nthe gate only logged, so claims continue; set window-gate=deny to enforce the next cutoff.\n\n"
    "## Read first\n- ledger:sw/tasks/t1\n\n"
    "<!-- handoff complete -->"
)
ON_LEDGER = (
    "Run window ended 25 October 12:00 UTC, gate log only. Tasks: 1 claimed, 1 done, 1 open. In flight: "
    "engineer@abc-0001 on t1. Claims continue; window-gate=deny enforces the next cutoff. Full handoff: "
    "master-watchdog."
)


def test_the_cutoff_logs_only_and_publishes_the_final_handoff(swarm):
    store, ledger = swarm
    _step(store, ledger, ALERT_AT)
    assert _step(store, ledger, END) == [
        "run window: observe at the cutoff: The run window ended at 25 October 12:00 UTC: new claims stop and 1 "
        "agents in flight hand off: engineer@abc-0001.",
        "run window: published the final state to the ledger and the watchdog",
    ]
    [row] = gate_log.recent(SLUG)
    assert {key: row[key] for key in ("at", "gate", "kind", "agent", "reason")} == {
        "at": END,
        "gate": "window",
        "kind": "observe",
        "agent": "swarm",
        "reason": "The run window ended at 25 October 12:00 UTC: new claims stop and 1 agents in flight hand off: "
        "engineer@abc-0001.",
    }
    assert _items(store, OWNER) == []
    assert run_window.claims_stopped(store.config(SLUG), END) == ""
    assert ledger.notices == [ON_LEDGER]
    assert _items(store, "master-watchdog") == [FINAL_OBSERVE]
    assert _step(store, ledger, END + MINUTE) == []
    assert len(gate_log.recent(SLUG)) == 1 and len(ledger.notices) == 1
    assert store.config(SLUG).window == WINDOW


def test_an_enforced_cutoff_stops_claims_and_asks_agents_to_hand_off(swarm):
    store, ledger = swarm
    actions = _step(store, ledger, END, gates={"window": "enforce"})
    assert actions[0].startswith("run window: deny at the cutoff: ")
    assert actions[1:] == [
        "run window: asked 1 agents in flight to hand off",
        "run window: published the final state to the ledger and the watchdog",
    ]
    assert [row["kind"] for row in gate_log.recent(SLUG)] == ["deny"]
    assert _items(store, OWNER) == [
        "The run window on swarm sw ended at 25 October 12:00 UTC. Finish the step you are on, push any commits with "
        "agentihooks swarm sw publish, write a Handoff v2 for task t1, run agentihooks swarm sw handoff <doc> and "
        "stop. No new task starts until swarm set changes the window."
    ]
    assert ledger.notices == [
        "Run window ended 25 October 12:00 UTC, gate deny. Tasks: 1 claimed, 1 done, 1 open. In flight: "
        "engineer@abc-0001 on t1. Agents in flight hand off and claims stay stopped. Full handoff: master-watchdog."
    ]
    config = store.config(SLUG)
    assert run_window.claims_stopped(config, END - 1) == ""
    assert run_window.claims_stopped(config, END) == (
        "the run window ended at 25 October 12:00 UTC, no new claims until swarm set changes the window"
    )
    assert run_window.claims_stopped(config, END + 600 * MINUTE) != ""
    assert _step(store, ledger, END + 600 * MINUTE) == []
    assert store.config(SLUG).window == WINDOW


def test_an_off_gate_skips_the_gate_and_still_publishes_the_final_handoff(swarm):
    store, ledger = swarm
    assert _step(store, ledger, END, gates={"window": "off"}) == [
        "run window: published the final state to the ledger and the watchdog",
    ]
    assert gate_log.recent(SLUG) == [] and _items(store, OWNER) == []
    assert ledger.notices == [
        "Run window ended 25 October 12:00 UTC, gate skip. Tasks: 1 claimed, 1 done, 1 open. In flight: "
        "engineer@abc-0001 on t1. Claims continue. Full handoff: master-watchdog."
    ]
    assert _items(store, "master@sw") == []
    assert run_window.claims_stopped(store.update(SLUG, gates={"window": "off"}), END) == ""


def test_only_swarm_set_extends_the_window_and_a_new_window_starts_fresh(swarm):
    store, ledger = swarm
    _step(store, ledger, END, gates={"window": "enforce"})
    config = store.update(SLUG, window={"start_ms": START, "minutes": 780})
    assert run_window.claims_stopped(config, END) == ""
    assert _step(store, ledger, END + MINUTE) == []
    assert store.redis.hget(store.key(SLUG, "run-window"), "cutoff") is None
    assert len(_step(store, ledger, END + 60 * MINUTE)) == 3


def test_the_tick_refuses_new_claims_after_an_enforced_cutoff(swarm):
    store, _ = swarm
    config = store.update(SLUG, gates={"window": "enforce"})

    class Runtime:
        def has_capacity(self, config):
            return True

    assert tick._spawn_stop(SLUG, config, store, Runtime(), END) == (
        "the run window ended at 25 October 12:00 UTC, no new claims until swarm set changes the window"
    )


def test_the_tick_runs_the_window_step(env, monkeypatch):  # noqa: F811
    store, ledger, rt = env
    from tests.swarm.test_delivery import FakeHerdr

    run("sw", "create", "--repo", "/repo")
    seen = []
    monkeypatch.setattr(tick.run_window, "step", lambda *args: seen.append(args) or ["window seen"])
    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    [(slug, config, got_store, _, doc, _)] = seen
    assert "window seen" in actions
    assert (slug, config.slug, got_store.redis) == ("sw", "sw", store.redis)
    assert doc["tasks"] == ledger.state("sw")["tasks"]


def test_a_bad_setting_raises_a_swarm_error():
    with pytest.raises(SwarmError) as caught:
        run_window.setting({}, "window-minutes", "-1", START)
    assert str(caught.value) == "window-minutes takes a whole number of minutes above 0"


class RefusingLedger(Ledger):
    def __init__(self):
        super().__init__()
        self.refuse = True

    def notify(self, slug, text):
        if self.refuse:
            self.refuse = False
            raise RuntimeError("ledger refused")
        super().notify(slug, text)


def test_a_failed_publish_retries_without_repeating_the_gate_or_the_watchdog(swarm):
    store, _ = swarm
    ledger = RefusingLedger()
    _step(store, ledger, ALERT_AT)
    with pytest.raises(RuntimeError):
        _step(store, ledger, END)
    marks = store.redis.hgetall(store.key(SLUG, "run-window"))
    assert ("cutoff" in marks, "published" in marks) == (True, False)
    assert _step(store, ledger, END + MINUTE) == [
        "run window: published the final state to the ledger and the watchdog"
    ]
    assert len(gate_log.recent(SLUG)) == 1
    assert _items(store, "master-watchdog") == [FINAL_OBSERVE]
    assert ledger.notices == [ON_LEDGER]
    assert _step(store, ledger, END + 2 * MINUTE) == []


def test_a_window_with_no_agent_in_flight_reads_none():
    text = run_window.final(SLUG, "observe", (START, END), {}, [])
    assert "## Done\nTasks by state: none.\n\n## Stopped at\nIn flight at the cutoff: none.\n\n" in text
    assert text.endswith("## Read first\nNone\n\n<!-- handoff complete -->")


def test_hub_admission_refuses_a_claim_after_an_enforced_cutoff(swarm):
    from scripts.hub.admission import Admission

    store, _ = swarm
    store.update(SLUG, gates={"window": "enforce"})

    class Source:
        def snapshot(self, slug):
            raise AssertionError("no snapshot after the cutoff")

    admission = Admission(store=store, ledger=object(), source=Source(), clock=lambda: END)
    assert admission.admit(SLUG, {"task": "t3", "lane": "eng", "generation": 0, "hive": "h1"}) is None
    assert store.claimant(SLUG, "t3") is None


def test_a_failed_gate_row_retries_without_asking_an_agent_twice(swarm, monkeypatch):
    store, ledger = swarm
    store.update(SLUG, gates={"window": "enforce"})
    real, calls = gate_log.append, []

    def append(slug, row, home=None):
        calls.append(row.kind)
        if len(calls) == 1:
            raise OSError("disk full")
        real(slug, row, home)

    monkeypatch.setattr(run_window.gate_log, "append", append)
    with pytest.raises(OSError):
        _step(store, ledger, END)
    assert store.redis.hget(store.key(SLUG, "run-window"), "cutoff") is None
    assert _step(store, ledger, END + MINUTE)[0].startswith("run window: deny at the cutoff: ")
    assert calls == ["deny", "deny"]
    assert [row["kind"] for row in gate_log.recent(SLUG)] == ["deny"]
    assert len(_items(store, OWNER)) == 1


@pytest.fixture
def away_zone(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_window_times_stay_in_utc_on_a_host_in_another_zone(away_zone):
    assert run_window.setting({}, "window-start", "2026-10-25T00:00:00Z", 0) == {"start_ms": START}
    assert run_window.final(SLUG, "off", (START, END), {}, []).startswith(
        "## Intent\nRun window on swarm sw, 25 October 00:00 UTC to 25 October 12:00 UTC, window gate skip.\n\n"
    )


def test_the_final_handoff_names_every_agent_in_flight():
    rows = {
        "t1": {"title": "Fix the claim", "state": "claimed"},
        "t4": {"title": "Ship it", "state": "pr"},
    }
    flying = [AgentRecord(OWNER, "eng", "t1"), AgentRecord("engineer@abc-0002", "eng", "t4")]
    assert run_window.final(SLUG, "observe", (START, END), rows, flying) == (
        "## Intent\nRun window on swarm sw, 25 October 00:00 UTC to 25 October 12:00 UTC, window gate log only.\n\n"
        "## Done\nTasks by state: 1 claimed, 1 pr.\n\n"
        "## Stopped at\nIn flight at the cutoff: engineer@abc-0001 on t1 Fix the claim; "
        "engineer@abc-0002 on t4 Ship it.\n\n"
        "## Decisions and promises\nOnly swarm set changes the window.\n\n"
        "## Next\nthe gate only logged, so claims continue; set window-gate=deny to enforce the next cutoff.\n\n"
        "## Read first\n- ledger:sw/tasks/t1\n- ledger:sw/tasks/t4\n\n"
        "<!-- handoff complete -->"
    )


def test_the_hand_off_ask_carries_its_task(swarm):
    from scripts.inbox.store import InboxStore

    store, ledger = swarm
    _step(store, ledger, END, gates={"window": "enforce"})
    assert [item.task for item in InboxStore(store.redis).inbox(OWNER)] == ["t1"]


def test_the_ledger_notice_fits_the_notifications_window():
    from scripts.swarm_ledger.ledger_notifications import TEXT_KEPT

    rows = {"t1": {"state": "claimed"}}
    blank = run_window.notice("observe", (START, END), rows, [AgentRecord("", "eng", "t1")])
    fits = [AgentRecord("e" * (TEXT_KEPT - len(blank)), "eng", "t1")]
    assert len(run_window.notice("observe", (START, END), rows, fits)) == TEXT_KEPT
    over = [AgentRecord("e" * (TEXT_KEPT - len(blank) + 1), "eng", "t1")]
    assert run_window.notice("observe", (START, END), rows, over) == (
        "Run window ended 25 October 12:00 UTC, gate log only. Tasks: 1 claimed. In flight: 1 agents. Claims "
        "continue; window-gate=deny enforces the next cutoff. Full handoff: master-watchdog."
    )


def test_the_ledger_notice_names_every_agent_in_flight():
    rows = {"t1": {"state": "claimed"}, "t4": {"state": "pr"}}
    flying = [AgentRecord(OWNER, "eng", "t1"), AgentRecord("engineer@abc-0002", "eng", "t4")]
    assert run_window.notice("off", (START, END), rows, flying) == (
        "Run window ended 25 October 12:00 UTC, gate skip. Tasks: 1 claimed, 1 pr. In flight: engineer@abc-0001 on "
        "t1, engineer@abc-0002 on t4. Claims continue. Full handoff: master-watchdog."
    )
