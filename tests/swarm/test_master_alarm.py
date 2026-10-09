import pytest

from scripts.doctor.priming import TEMPLATE
from scripts.inbox.store import InboxStore
from scripts.swarm import master_alarm, master_start, tick_master
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks, workers  # noqa: F401
from tests.swarm.test_tick_master import BROKEN, DOWN, ENGINEER, BrokenMaster

pytestmark = pytest.mark.xdist_group("fakeredis")
DOCTOR = "sw-doctor"
DOCTOR_MASTER = f"master@{DOCTOR}"


def doctored(store):  # noqa: F811
    store.create(SwarmConfig(DOCTOR, "/repo", max_eng=0, max_ci=0, template=TEMPLATE))
    store.set_peer("sw", DOCTOR)
    store.set_peer(DOCTOR, "sw")
    return store


def mail(store, address):  # noqa: F811
    return [(item.sender, item.text) for item in InboxStore(store.redis).inbox(address)]


def notices(store, name):  # noqa: F811
    return [text for sender, text in mail(store, name) if text.startswith("The master of swarm sw is down")]


def broken_outage(store):  # noqa: F811
    runtime, ledger = BrokenMaster(), tasks(("t1", "eng"), ("t2", "ci"))
    for at in (1, 2, 3, 130_000):
        tick("sw", store, ledger, runtime, at)
    return runtime, ledger


def test_a_failed_launch_tells_the_doctor_master_at_once_with_the_exact_error(store):  # noqa: F811
    doctored(store)
    runtime, ledger = BrokenMaster(), tasks(("t1", "eng"))
    actions = tick("sw", store, ledger, runtime, 1)
    assert mail(store, DOCTOR_MASTER) == [("swarm", master_alarm.DOCTOR.format(slug="sw", error=BROKEN))]
    assert f"told the Doctor {DOCTOR} the master launch failed: {BROKEN}" in actions


def test_every_live_agent_gets_one_notice_on_a_failed_launch(store):  # noqa: F811
    runtime, ledger = broken_outage(doctored(store))
    names = sorted(a.name for a in workers(store))
    assert len(names) == 2
    for name in names:
        assert notices(store, name) == [master_alarm.NOTICE.format(slug="sw", error=BROKEN, name=name)]


def test_the_alarm_is_sent_once_per_outage_not_every_tick(store):  # noqa: F811
    runtime, ledger = broken_outage(doctored(store))
    for at in range(1 + DOWN, 3 * DOWN, 60_000):
        tick("sw", store, ledger, runtime, at)
    assert runtime.master_spawns > 2
    assert len(mail(store, DOCTOR_MASTER)) == 1
    for agent in workers(store):
        assert len(notices(store, agent.name)) == 1


def test_a_master_that_binds_ends_the_outage_so_the_next_one_alarms_again(store):  # noqa: F811
    runtime, ledger = broken_outage(doctored(store))
    runtime.broken = False
    tick("sw", store, ledger, runtime, 1 + DOWN)
    assert [m.state for m in masters(store)] == ["working"]
    assert master_alarm.read(store, "sw") == {}
    master = masters(store)[0]
    runtime.broken = True
    runtime.live.discard(master.name)
    store.drop_agent("sw", master.name)
    tick("sw", store, ledger, runtime, 2 + DOWN)
    texts = [text for _, text in mail(store, DOCTOR_MASTER)]
    assert texts == [
        master_alarm.DOCTOR.format(slug="sw", error=BROKEN),
        master_alarm.BACK.format(slug="sw"),
        master_alarm.DOCTOR.format(slug="sw", error=BROKEN),
    ]
    assert all(len(notices(store, agent.name)) == 2 for agent in workers(store))
    assert all(
        mail(store, agent.name).count(("swarm", master_alarm.BACK.format(slug="sw"))) == 1 for agent in workers(store)
    )


def test_an_agent_that_goes_live_during_the_outage_is_told_when_it_goes_live(store):  # noqa: F811
    runtime, ledger = BrokenMaster(), tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    ledger.rows["t9"] = {"id": "t9", "lane": "eng", "state": "open", "claimed_by": "", "out_of_scope": False}
    tick("sw", store, ledger, runtime, 3)
    late = next(a for a in workers(store) if a.task == "t9")
    assert notices(store, late.name) == [master_alarm.NOTICE.format(slug="sw", error=BROKEN, name=late.name)]
    tick("sw", store, ledger, runtime, 1 + DOWN)
    assert len(notices(store, late.name)) == 1
    assert all(len(notices(store, agent.name)) == 1 for agent in workers(store))


def test_a_peer_that_is_not_a_doctor_gets_nothing(store):  # noqa: F811
    store.create(SwarmConfig("other", "/repo", max_eng=0, max_ci=0))
    store.set_peer("sw", "other")
    broken_outage(store)
    assert mail(store, "master@other") == []


def test_the_alarm_state_is_saved_before_any_item(store, monkeypatch):  # noqa: F811
    doctored(store)
    runtime = FakeRuntime()
    runtime.live.add(ENGINEER)
    store.put_agent("sw", AgentRecord(ENGINEER, "eng", "t1", state="working", seat="eng-1@sw"))
    master_alarm.failed(store, "sw", BROKEN, 1)
    seen, real = [], InboxStore.send

    def send(self, *args, **kwargs):
        seen.append(master_alarm.read(store, "sw"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(InboxStore, "send", send)
    master_alarm.run("sw", store, runtime, "")
    assert seen == [{"error": BROKEN, "at": 1, "doctor": DOCTOR, "told": [ENGINEER]}] * 2


def test_nothing_is_sent_without_a_recorded_failure(store):  # noqa: F811
    doctored(store)
    assert master_alarm.run("sw", store, FakeRuntime(), "") == []
    assert mail(store, DOCTOR_MASTER) == []


def test_a_launch_with_no_hook_alarms_with_the_master_name(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_MASTER_DOWN_MINUTES", "60")
    doctored(store)

    class Silent(FakeRuntime):
        def reported(self, agent):
            return agent.lane != MASTER

    runtime, ledger = Silent(), tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, 1)
    name = masters(store)[0].name
    assert mail(store, DOCTOR_MASTER) == []
    tick("sw", store, ledger, runtime, master_start.DEADLINE_MS + 1)
    error = master_start.NO_HOOK.format(name=name)
    assert mail(store, DOCTOR_MASTER) == [("swarm", master_alarm.DOCTOR.format(slug="sw", error=error))]
    assert notices(store, workers(store)[0].name) == [
        master_alarm.NOTICE.format(slug="sw", error=error, name=workers(store)[0].name)
    ]


def test_the_notice_tells_an_agent_to_keep_working_and_not_wait():
    text = master_alarm.NOTICE.format(slug="sw", error=BROKEN, name="engineer@a1b2c3-0001")
    assert BROKEN in text
    assert "own task through to merge" in text
    assert "agentihooks ledger --slug sw --as engineer@a1b2c3-0001 question add" in text
    assert "do not wait on master replies" in text


def test_the_promoted_prompt_names_the_exact_error_and_the_fix_path(store):  # noqa: F811
    runtime, ledger = broken_outage(store)
    tick("sw", store, ledger, runtime, 1 + DOWN)
    engineer = next(a for a in workers(store) if a.lane == "eng")
    promoted = [text for _, text in mail(store, engineer.name) if text.startswith("PROMOTED")]
    assert promoted == [tick_master.prompt("sw", engineer, BROKEN, 5, BROKEN)]
    assert f"The exact launch error: {BROKEN}." in promoted[0]
    assert "agentihooks swarm sw master up --new" in promoted[0]
    assert "find where that error is raised" in promoted[0]


def test_the_promoted_engineer_gets_no_master_down_notice(store):  # noqa: F811
    runtime = FakeRuntime()
    runtime.live.add(ENGINEER)
    store.put_agent("sw", AgentRecord(ENGINEER, "eng", "t1", state="working", seat="eng-1@sw"))
    master_alarm.failed(store, "sw", BROKEN, 1)
    assert master_alarm.run("sw", store, runtime, ENGINEER) == []
    assert notices(store, ENGINEER) == []


def test_a_forced_launch_that_fails_first_alarms_the_doctor_and_the_unpromoted_agents(store):  # noqa: F811
    doctored(store)
    runtime, ledger = BrokenMaster(), tasks(("t1", "eng"), ("t2", "ci"))
    runtime.broken = False
    tick("sw", store, ledger, runtime, 1)
    master = masters(store)[0]
    runtime.full = True
    runtime.live.discard(master.name)
    store.drop_agent("sw", master.name)
    tick("sw", store, ledger, runtime, 2)
    assert mail(store, DOCTOR_MASTER) == []
    runtime.full, runtime.broken = False, True
    actions = tick("sw", store, ledger, runtime, 2 + DOWN)
    assert "master down 5 minutes, forced a master launch" in actions
    assert mail(store, DOCTOR_MASTER) == [("swarm", master_alarm.DOCTOR.format(slug="sw", error=BROKEN))]
    engineer = next(a for a in workers(store) if a.lane == "eng")
    ci = next(a for a in workers(store) if a.lane == "ci")
    assert tick_master.promoted(store, "sw") == engineer.name
    assert notices(store, engineer.name) == []
    assert notices(store, ci.name) == [master_alarm.NOTICE.format(slug="sw", error=BROKEN, name=ci.name)]


def test_a_forced_launch_refused_for_capacity_names_its_own_error_not_an_older_one(store):  # noqa: F811
    runtime, ledger = broken_outage(store)
    runtime.full = True
    tick("sw", store, ledger, runtime, 1 + DOWN)
    engineer = next(a for a in workers(store) if a.lane == "eng")
    waiting = "no session slot for the master, waiting"
    promoted = [text for _, text in mail(store, engineer.name) if text.startswith("PROMOTED")]
    assert promoted == [tick_master.prompt("sw", engineer, waiting, 5, waiting)]


def test_a_forced_launch_with_no_hook_names_the_silent_master(store):  # noqa: F811
    runtime, ledger = broken_outage(store)
    runtime.broken, runtime.registered = False, set()
    tick("sw", store, ledger, runtime, 1 + DOWN)
    silent = runtime.masters[0][0]
    tick("sw", store, ledger, runtime, 1 + DOWN + 120_000)
    engineer = next(a for a in workers(store) if a.lane == "eng")
    promoted = [text for _, text in mail(store, engineer.name) if text.startswith("PROMOTED")]
    error = master_start.NO_HOOK.format(name=silent)
    assert promoted == [tick_master.prompt("sw", engineer, tick_master.NO_HOOK, 5, error)]


def test_a_stopping_swarm_drops_the_alarm_without_any_notice(store):  # noqa: F811
    runtime, ledger = broken_outage(doctored(store))
    names = [a.name for a in workers(store)]
    store.update("sw", state="stopping")
    tick("sw", store, ledger, runtime, 1 + DOWN)
    assert master_alarm.read(store, "sw") == {}
    assert [text for _, text in mail(store, DOCTOR_MASTER)] == [master_alarm.DOCTOR.format(slug="sw", error=BROKEN)]
    back = ("swarm", master_alarm.BACK.format(slug="sw"))
    assert all(back not in mail(store, name) and len(notices(store, name)) == 1 for name in names)


def test_a_stopping_swarm_sends_no_alarm_while_its_master_pass_is_skipped(store):  # noqa: F811
    doctored(store)
    runtime = FakeRuntime()
    runtime.live.add(ENGINEER)
    store.put_agent("sw", AgentRecord(ENGINEER, "eng", "t1", state="working", seat="eng-1@sw"))
    master_alarm.failed(store, "sw", BROKEN, 1)
    store.update("sw", state="stopping")
    assert master_alarm.run("sw", store, runtime, "") == []
    assert mail(store, DOCTOR_MASTER) == []
