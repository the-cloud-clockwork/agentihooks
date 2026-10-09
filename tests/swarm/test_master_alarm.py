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
    assert len(mail(store, DOCTOR_MASTER)) == 2
    assert all(len(notices(store, agent.name)) == 2 for agent in workers(store))


def test_an_agent_that_goes_live_during_the_outage_is_told_at_the_next_failure(store):  # noqa: F811
    runtime, ledger = BrokenMaster(), tasks(("t1", "eng"))
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    ledger.rows["t9"] = {"id": "t9", "lane": "eng", "state": "open", "claimed_by": "", "out_of_scope": False}
    tick("sw", store, ledger, runtime, 3)
    late = next(a for a in workers(store) if a.task == "t9")
    assert notices(store, late.name) == []
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
    master_alarm.failed(store, "sw", BROKEN)
    seen, real = [], InboxStore.send

    def send(self, *args, **kwargs):
        seen.append(master_alarm.read(store, "sw"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(InboxStore, "send", send)
    master_alarm.run("sw", store, runtime)
    assert seen == [{"error": BROKEN, "pending": False, "doctor": DOCTOR, "told": [ENGINEER]}] * 2


def test_nothing_is_sent_without_a_recorded_failure(store):  # noqa: F811
    doctored(store)
    assert master_alarm.run("sw", store, FakeRuntime()) == []
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
