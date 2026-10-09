import pytest

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import master_alarm, tick_master
from scripts.swarm.status import status_report
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks, workers  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")
DOWN = 5 * 60 * 1000
ENGINEER = "engineer@a1b2c3-0001"
DOC = {"tasks": [], "_meta": {"events": []}}
BROKEN = "master spawn failed: MCP_KEY_GATEWAY is unset"
MASTER_SEAT = seat_address("sw", MASTER)
NOTICE = master_alarm.NOTICE.format(slug="sw", error=BROKEN, name=ENGINEER)


class BrokenMaster(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.broken = True
        self.registered = None
        self.master_spawns = 0

    def spawn(self, config, lane, name, task):
        if lane == MASTER:
            self.master_spawns += 1
            if self.broken:
                raise SpawnError("MCP_KEY_GATEWAY is unset")
        return super().spawn(config, lane, name, task)

    def reported(self, agent):
        return agent.name in (self.live if self.registered is None else self.registered)


def outage(store):  # noqa: F811
    runtime, ledger = BrokenMaster(), tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, 1)
    for at in (2, 3, 130_000):
        tick("sw", store, ledger, runtime, at)
    return runtime, ledger


def from_forced(actions, count):
    start = actions.index("master down 5 minutes, forced a master launch")
    return actions[start : start + count]


def mail(store, name):  # noqa: F811
    return [(item.sender, item.text) for item in InboxStore(store.redis).inbox(name)]


def events(store, seat):  # noqa: F811
    return [(e["event"], e["detail"], e["at"]) for e in store.seats.history(seat) if "event" in e]


def test_no_force_and_no_promotion_inside_the_down_window(store):  # noqa: F811
    runtime, ledger = outage(store)
    actions = tick("sw", store, ledger, runtime, DOWN)
    assert not any("forced" in a or "promoted" in a for a in actions)
    assert tick_master.read(store, "sw") == {"since": 1}
    assert runtime.master_spawns == 2


def test_the_startup_retry_stays_bounded_inside_the_down_window(store):  # noqa: F811
    runtime, ledger = outage(store)
    for at in range(130_001, DOWN, 30_000):
        tick("sw", store, ledger, runtime, at)
    assert runtime.master_spawns == 2


def test_a_master_down_five_minutes_is_launched_first_and_no_engineer_is_promoted(store):  # noqa: F811
    runtime, ledger = outage(store)
    runtime.broken = False
    actions = tick("sw", store, ledger, runtime, 1 + DOWN)
    assert from_forced(actions, 2) == [
        "master down 5 minutes, forced a master launch",
        f"spawned master {runtime.masters[0][0]}",
    ]
    assert [m.state for m in masters(store)] == ["working"]
    assert tick_master.read(store, "sw") == {}
    assert mail(store, ENGINEER) == [("swarm", NOTICE)]


def test_a_failed_forced_launch_promotes_one_live_engineer(store):  # noqa: F811
    runtime, ledger = outage(store)
    actions = tick("sw", store, ledger, runtime, 1 + DOWN)
    assert from_forced(actions, 4) == [
        "master down 5 minutes, forced a master launch",
        BROKEN,
        f"promoted {ENGINEER} to restore the master: {BROKEN}",
        f"sent {ENGINEER} the promoted prompt",
    ]
    assert tick_master.read(store, "sw") == {"since": 1, "forced_at": 1 + DOWN, "failure": BROKEN, "promoted": ENGINEER}
    assert mail(store, ENGINEER) == [
        ("swarm", NOTICE),
        ("swarm", tick_master.prompt("sw", workers(store)[0], BROKEN, 5, BROKEN)),
    ]
    assert ledger.notes[-1] == tick_master.PROMOTED_NOTICE.format(minutes=5)
    seat = workers(store)[0].seat
    assert events(store, seat) == [("promoted", BROKEN, 1 + DOWN), ("message", "the promoted prompt", 1 + DOWN)]
    assert events(store, MASTER_SEAT) == [("promoted", f"{ENGINEER}: {BROKEN}", 1 + DOWN)]
    tick("sw", store, ledger, runtime, 2 + DOWN)
    assert len(mail(store, ENGINEER)) == 2


def test_the_promotion_is_saved_before_its_notice(store):  # noqa: F811
    import contextlib

    from scripts.swarm.store import SwarmError

    runtime, ledger = outage(store)

    def notify(slug, text):
        raise SwarmError("ledger sw: connection reset")

    ledger.notify = notify
    with contextlib.suppress(SwarmError):
        tick("sw", store, ledger, runtime, 1 + DOWN)
    assert tick_master.read(store, "sw")["promoted"] == ENGINEER
    with contextlib.suppress(SwarmError):
        tick("sw", store, ledger, runtime, 2 + DOWN)
    assert len(mail(store, ENGINEER)) == 2


def test_nobody_told_is_saved_before_its_notice(store):  # noqa: F811
    import contextlib

    from scripts.swarm.store import SwarmError

    runtime, ledger = outage(store)
    runtime.live.discard(ENGINEER)
    store.drop_agent("sw", ENGINEER)
    ledger.rows["t1"]["state"] = "done"

    def notify(slug, text):
        raise SwarmError("ledger sw: connection reset")

    ledger.notify = notify
    with contextlib.suppress(SwarmError):
        tick("sw", store, ledger, runtime, 1 + DOWN)
    assert tick_master.read(store, "sw")["nobody_told"] is True


def test_a_finished_master_record_does_not_hold_off_the_promotion(store):  # noqa: F811
    runtime, ledger = outage(store)
    store.put_agent("sw", AgentRecord("master@a1b2c3-0099", MASTER, MASTER, state="finished", seat=MASTER_SEAT))
    actions = tick("sw", store, ledger, runtime, 1 + DOWN)
    assert f"promoted {ENGINEER} to restore the master: {BROKEN}" in actions


def test_swarm_status_marks_the_promoted_engineer(store):  # noqa: F811
    runtime, ledger = outage(store)
    tick("sw", store, ledger, runtime, 1 + DOWN)
    report = status_report(store, "sw", DOC)
    assert report["promotion"]["promoted"] == ENGINEER
    assert [(a["name"], a["promoted"]) for a in report["agents"]] == [(ENGINEER, True)]


def test_the_promoted_prompt_states_the_purpose_in_order(store):  # noqa: F811
    runtime, ledger = outage(store)
    agent = workers(store)[0]
    assert tick_master.prompt("sw", agent, "master spawn failed: boom", 5, "master spawn failed: boom") == (
        "PROMOTED: swarm sw has had no live master for 5 minutes and the forced master launch failed: "
        "master spawn failed: boom. The exact launch error: master spawn failed: boom. You stay "
        "engineer@a1b2c3-0001 on task t1, but until a master binds your only purpose is, in this order: "
        "1. Bring the master back as soon as possible. The tick forces a new master launch every 5 minutes; read why "
        "it fails with agentihooks swarm sw status and journalctl --user -u agentihooks-swarm.service. "
        "2. Fix the causes of the outage right away in code: find where that error is raised and fix it through your "
        "own pull request into dev, never as follow ups. Once it merges and wt.sh done syncs local dev, the next "
        "forced launch runs the fixed code, or start one at once with agentihooks swarm sw master up --new. "
        "3. Throughout, tell the swarm and the operator what failed and what you are doing, with agentihooks swarm "
        'sw say "<text>" and agentihooks swarm sw say --to eng "<text>". '
        "You do not act as master: never answer chat as master, write tasks or steer the swarm. "
        "The tick ends this promotion when a real master binds and tells you; then return to task t1."
    )


def test_a_master_that_binds_ends_the_promotion_and_hands_back(store):  # noqa: F811
    runtime, ledger = outage(store)
    tick("sw", store, ledger, runtime, 1 + DOWN)
    runtime.broken = False
    actions = tick("sw", store, ledger, runtime, 1 + 2 * DOWN)
    master = runtime.masters[0][0]
    assert from_forced(actions, 4) == [
        "master down 5 minutes, forced a master launch",
        f"spawned master {master}",
        f"master {master} bound, ended the promotion of {ENGINEER}",
        f"sent {ENGINEER} the hand back",
    ]
    assert tick_master.read(store, "sw") == {}
    assert mail(store, ENGINEER)[-1] == ("swarm", tick_master.HAND_BACK.format(master=master, task="t1"))
    assert ledger.notes[-1] == tick_master.HANDED_BACK_NOTICE
    seat = workers(store)[0].seat
    assert events(store, seat)[-2:] == [
        ("handed back", master, 1 + 2 * DOWN),
        ("message", "the hand back", 1 + 2 * DOWN),
    ]
    assert events(store, MASTER_SEAT)[-1] == ("handed back", f"{ENGINEER}: {master}", 1 + 2 * DOWN)
    assert [a["promoted"] for a in status_report(store, "sw", DOC)["agents"]] == [False, False]


def test_a_forced_launch_with_no_hook_promotes_after_the_startup_deadline(store):  # noqa: F811
    runtime, ledger = outage(store)
    runtime.broken, runtime.registered = False, set()
    tick("sw", store, ledger, runtime, 1 + DOWN)
    assert tick_master.promoted(store, "sw") == ""
    tick("sw", store, ledger, runtime, DOWN + 120_000)
    assert tick_master.promoted(store, "sw") == ""
    actions = tick("sw", store, ledger, runtime, 1 + DOWN + 120_000)
    assert f"promoted {ENGINEER} to restore the master: {tick_master.NO_HOOK}" in actions


def test_a_gone_promoted_engineer_is_replaced(store):  # noqa: F811
    runtime, ledger = outage(store)
    ledger.rows["t2"] = {"id": "t2", "lane": "eng", "state": "open", "claimed_by": "", "out_of_scope": False}
    tick("sw", store, ledger, runtime, DOWN)
    tick("sw", store, ledger, runtime, 1 + DOWN)
    second = next(a for a in workers(store) if a.name != ENGINEER)
    gone = next(a for a in workers(store) if a.name == ENGINEER)
    runtime.live.discard(ENGINEER)
    actions = tick("sw", store, ledger, runtime, 2 + DOWN)
    assert f"promoted {ENGINEER} is gone" in actions
    assert tick_master.promoted(store, "sw") == second.name
    assert events(store, gone.seat)[-1] == ("promotion lost", "the promoted engineer is gone", 2 + DOWN)
    assert events(store, MASTER_SEAT)[-2] == ("promotion lost", ENGINEER, 2 + DOWN)


def test_a_promoted_engineer_whose_record_is_gone_leaves_nobody_promoted(store):  # noqa: F811
    runtime, ledger = outage(store)
    tick("sw", store, ledger, runtime, 1 + DOWN)
    seat = workers(store)[0].seat
    runtime.live.discard(ENGINEER)
    store.drop_agent("sw", ENGINEER)
    ledger.rows["t1"]["state"] = "done"
    actions = tick("sw", store, ledger, runtime, 2 + DOWN)
    assert f"promoted {ENGINEER} is gone" in actions
    assert "no live engineer to promote" in actions
    assert tick_master.read(store, "sw") == {
        "since": 1,
        "forced_at": 1 + DOWN,
        "failure": BROKEN,
        "nobody_told": True,
    }
    assert events(store, seat)[-1] == ("message", "the promoted prompt", 1 + DOWN)


def test_no_live_engineer_is_told_once(store):  # noqa: F811
    runtime, ledger = outage(store)
    runtime.live.discard(ENGINEER)
    store.drop_agent("sw", ENGINEER)
    ledger.rows["t1"]["state"] = "done"
    actions = tick("sw", store, ledger, runtime, 1 + DOWN)
    assert "no live engineer to promote" in actions
    tick("sw", store, ledger, runtime, 2 + DOWN)
    assert ledger.notes.count(tick_master.NOBODY_NOTICE.format(minutes=5)) == 1
    assert tick_master.read(store, "sw")["nobody_told"] is True


@pytest.mark.parametrize("state", ["finished", "awaiting-decision"])
def test_a_finished_or_awaiting_engineer_is_never_promoted(state):
    swarm, runtime = direct(AgentRecord(ENGINEER, "eng", "t1", state=state, seat="eng-1@sw"))
    actions = tick_master.run("sw", swarm.config("sw"), swarm, tasks(), runtime, 1 + DOWN, lambda: [BROKEN])
    assert actions == ["master down 5 minutes, forced a master launch", BROKEN, "no live engineer to promote"]


def direct(*agents):
    import fakeredis

    swarm = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    swarm.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0))
    runtime = FakeRuntime()
    for agent in agents:
        swarm.put_agent("sw", agent)
        runtime.live.add(agent.name)
    tick_master._save(swarm, "sw", {"since": 1})
    return swarm, runtime


def test_a_master_starting_before_any_forced_launch_is_not_a_failure():
    swarm, runtime = direct(AgentRecord("master@a1b2c3-0002", MASTER, MASTER, state="starting", seat=MASTER_SEAT))
    actions = tick_master.run("sw", swarm.config("sw"), swarm, tasks(), runtime, DOWN, lambda: [])
    assert actions == []
    assert tick_master.read(swarm, "sw") == {"since": 1}


def test_a_finished_master_record_counts_as_no_master():
    engineer = AgentRecord(ENGINEER, "eng", "t1", state="working", seat="eng-1@sw")
    finished = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, state="finished", seat=MASTER_SEAT)
    swarm, runtime = direct(engineer, finished)
    actions = tick_master.run("sw", swarm.config("sw"), swarm, tasks(), runtime, 1 + DOWN, lambda: [BROKEN])
    assert actions == [
        "master down 5 minutes, forced a master launch",
        BROKEN,
        f"promoted {ENGINEER} to restore the master: {BROKEN}",
        f"sent {ENGINEER} the promoted prompt",
    ]


def test_every_launch_action_names_the_failure(store):  # noqa: F811
    runtime, ledger = outage(store)
    tick_master.run("sw", store.config("sw"), store, ledger, runtime, 1 + DOWN, lambda: ["first", "second"])
    assert tick_master.read(store, "sw")["failure"] == "first; second"


def test_the_down_window_comes_from_the_setting(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_MASTER_DOWN_MINUTES", "1")
    runtime, ledger = BrokenMaster(), tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, 1)
    actions = tick("sw", store, ledger, runtime, 1 + 60_000)
    assert "master down 1 minutes, forced a master launch" in actions


def test_stopping_ends_the_promotion_without_forcing(store):  # noqa: F811
    runtime, ledger = outage(store)
    tick("sw", store, ledger, runtime, 1 + DOWN)
    store.update("sw", state="stopping")
    actions = tick("sw", store, ledger, runtime, 1 + 2 * DOWN)
    assert not any("forced" in a for a in actions)
    assert f"the swarm is stopping, ended the promotion of {ENGINEER}" in actions
    assert tick_master.read(store, "sw") == {}
    assert mail(store, ENGINEER)[-1] == ("swarm", tick_master.STOPPED.format(task="t1"))
    assert ledger.notes[-1] == tick_master.STOPPED_NOTICE
    assert events(store, workers(store)[0].seat)[-2:] == [
        ("handed back", "the swarm is stopping", 1 + 2 * DOWN),
        ("message", "the hand back", 1 + 2 * DOWN),
    ]
    assert events(store, MASTER_SEAT)[-1] == ("handed back", f"{ENGINEER}: the swarm is stopping", 1 + 2 * DOWN)


def test_the_status_line_names_the_promoted_engineer():
    state = {"since": 1, "failure": "master spawn failed: boom", "promoted": ENGINEER}
    assert tick_master.status_line(state) == f"promoted  {ENGINEER}  restoring the master: master spawn failed: boom"
    assert tick_master.status_line({"since": 1}) == ""
