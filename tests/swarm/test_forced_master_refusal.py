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
        "sw", config, swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001)
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
        AgentRecord("master@a1b2c3-0004", MASTER, MASTER, state="starting", seat="master@sw"),
    )
    runtime.live.clear()
    config = swarm.config("sw")
    actions = tick_master.run(
        "sw", config, swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001)
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

    actions = tick_master.run("sw", swarm.config("sw"), swarm, tasks(), runtime, 300_001, launch)

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
            "sw", config, swarm, tasks(), runtime, 300_000, lambda: _master("sw", config, swarm, runtime, 300_000)
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
        "sw", config, swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001)
    )

    assert len(runtime.masters) == 1
    name = runtime.masters[0][0]
    assert actions == ["master down 5 minutes, forced a master launch", f"spawned master {name}"]
    assert tick_master.read(swarm, "sw") == {}
