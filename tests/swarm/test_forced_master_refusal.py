import pytest

from scripts.swarm import tick_master


def test_a_forced_launch_finishes_a_dead_suspect_lead_and_launches_its_replacement():
    from dataclasses import replace

    from scripts.swarm.store import MASTER, AgentRecord
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    lead = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, state="suspect", seat="master@sw")
    swarm, runtime = direct(lead)
    runtime.live.clear()
    config = swarm.config("sw")
    actions = tick_master.run(
        "sw", swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001), {}
    )

    assert len(runtime.masters) == 1
    name = runtime.masters[0][0]
    assert actions == [
        "master down 5 minutes, forced a master launch",
        "finished dead lead master@a1b2c3-0002 (suspect)",
        f"spawned master {name}",
    ]
    assert replace(lead, state="finished") in swarm.agents("sw")


def test_a_forced_launch_lists_each_unfinished_lead_with_its_state():
    from scripts.swarm.store import MASTER, AgentRecord
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    swarm, runtime = direct(
        AgentRecord("master@a1b2c3-0002", MASTER, MASTER, state="suspect", seat="master@sw"),
        AgentRecord("master@a1b2c3-0003", MASTER, MASTER, state="retiring", seat="master@sw"),
        AgentRecord("master@a1b2c3-0004", MASTER, MASTER, started_at=200_000, state="starting", seat="master@sw"),
    )
    runtime.live.clear()
    config = swarm.config("sw")
    actions = tick_master.run(
        "sw", swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001), {}
    )

    assert actions == [
        "master down 5 minutes, forced a master launch",
        "finished dead lead master@a1b2c3-0002 (suspect)",
        "master launch refused: unfinished lead master@a1b2c3-0003 (retiring), master@a1b2c3-0004 (starting)",
    ]
    assert [a.state for a in swarm.agents("sw")] == ["finished", "retiring", "starting"]
    assert runtime.masters == []


@pytest.mark.parametrize(
    ("pending", "reason"),
    [
        ({"name": "master@a1b2c3-0003"}, "master launch refused: startup pending for master@a1b2c3-0003"),
        ({"alerted": True}, "master launch refused: startup retry backoff pending"),
        ({}, "no master launched"),
    ],
)
def test_a_forced_launch_reports_startup_state_recorded_during_the_launch(pending, reason):
    from scripts.swarm import master_start
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    swarm, runtime = direct()

    def launch():
        master_start.save(swarm, "sw", pending)
        return []

    actions = tick_master.run("sw", swarm, tasks(), runtime, 300_001, launch, {})

    assert actions == ["master down 5 minutes, forced a master launch", reason, "no live engineer to promote"]
    assert tick_master.read(swarm, "sw")["failure"] == reason
    assert master_start.read(swarm, "sw") == pending
    assert runtime.masters == []


def test_an_ordinary_tick_does_not_report_an_unfinished_lead_as_a_forced_refusal():
    from scripts.swarm.store import MASTER, AgentRecord
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    swarm, runtime = direct(AgentRecord("master@a1b2c3-0002", MASTER, MASTER, state="suspect", seat="master@sw"))
    runtime.live.clear()
    config = swarm.config("sw")

    assert (
        tick_master.run(
            "sw", swarm, tasks(), runtime, 300_000, lambda: _master("sw", config, swarm, runtime, 300_000), {}
        )
        == []
    )
    assert tick_master.read(swarm, "sw") == {"since": 1}
    assert [a.state for a in swarm.agents("sw")] == ["suspect"]


def test_a_successful_forced_launch_reports_placement_without_a_refusal():
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    swarm, runtime = direct()
    config = swarm.config("sw")
    actions = tick_master.run(
        "sw", swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001), {}
    )

    assert len(runtime.masters) == 1
    name = runtime.masters[0][0]
    assert actions == ["master down 5 minutes, forced a master launch", f"spawned master {name}"]
    assert tick_master.read(swarm, "sw") == {}


def test_a_forced_launch_finishes_a_dead_starting_lead_past_its_deadline_and_launches_its_replacement():
    from dataclasses import replace

    from scripts.swarm.store import MASTER, AgentRecord
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    lead = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, started_at=180_001, state="starting", seat="master@sw")
    swarm, runtime = direct(lead)
    runtime.live.clear()
    config = swarm.config("sw")
    actions = tick_master.run(
        "sw", swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001), {}
    )

    name = runtime.masters[0][0]
    assert actions == [
        "master down 5 minutes, forced a master launch",
        "finished dead lead master@a1b2c3-0002 (starting)",
        f"spawned master {name}",
    ]
    assert replace(lead, state="finished") in swarm.agents("sw")


def test_a_dead_starting_lead_inside_its_deadline_still_holds_the_forced_launch():
    from scripts.swarm.store import MASTER, AgentRecord
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    lead = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, started_at=180_002, state="starting", seat="master@sw")
    swarm, runtime = direct(lead)
    runtime.live.clear()
    actions = tick_master.run("sw", swarm, tasks(), runtime, 300_001, lambda: [], {})

    assert actions[:2] == [
        "master down 5 minutes, forced a master launch",
        "master launch refused: unfinished lead master@a1b2c3-0002 (starting)",
    ]
    assert [a.state for a in swarm.agents("sw")] == ["starting"]


def ledger_doc(by: str, at: int) -> dict:
    return {"tasks": [], "_meta": {"events": [{"kind": "comment", "by": by, "at": at}]}}


def test_a_live_starting_lead_that_wrote_the_ledger_since_launch_ends_the_outage():
    from scripts.swarm.store import MASTER, AgentRecord
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    lead = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, started_at=5, state="starting", seat="master@sw")
    swarm, runtime = direct(lead)
    actions = tick_master.run("sw", swarm, tasks(), runtime, 300_001, lambda: [], ledger_doc(lead.name, 5))

    assert actions == []
    assert tick_master.read(swarm, "sw") == {}
    assert [a.state for a in swarm.agents("sw")] == ["starting"]


@pytest.mark.parametrize(
    ("by", "at", "live"),
    [("master@a1b2c3-0002", 4, True), ("engineer@a1b2c3-0001", 5, True), ("master@a1b2c3-0002", 5, False)],
)
def test_a_starting_lead_without_a_ledger_write_since_launch_or_a_live_process_is_not_seated(by, at, live):
    from scripts.swarm import master_start
    from scripts.swarm.store import MASTER, AgentRecord

    lead = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, started_at=5, state="starting", seat="master@sw")
    names = {lead.name} if live else set()

    assert master_start.seated(lead, names, ledger_doc(by, at)) is False
    assert master_start.seated(lead, {lead.name}, ledger_doc(lead.name, 5)) is True


def test_a_launched_at_time_is_the_launch_a_ledger_write_must_follow():
    from dataclasses import replace

    from scripts.swarm import master_start
    from scripts.swarm.store import MASTER, AgentRecord

    lead = AgentRecord("master@a1b2c3-0002", MASTER, MASTER, started_at=5, launched_at=9, state="starting")

    assert master_start.seated(lead, {lead.name}, ledger_doc(lead.name, 8)) is False
    assert master_start.seated(lead, {lead.name}, ledger_doc(lead.name, 9)) is True
    assert master_start.seated(replace(lead, state="working"), {lead.name}, {}) is True
    assert master_start.seated(replace(lead, state="suspect"), {lead.name}, ledger_doc(lead.name, 9)) is False
