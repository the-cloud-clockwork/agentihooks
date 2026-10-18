import pytest

from scripts.swarm import tick_master

pytestmark = pytest.mark.xdist_group("fakeredis")


def test_a_forced_launch_reports_the_unfinished_lead_that_blocks_replacement():
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

    assert actions == [
        "master down 5 minutes, forced a master launch",
        "master launch refused: unfinished lead master@a1b2c3-0002 (suspect)",
    ]
    assert swarm.agents("sw") == [lead]
    assert runtime.masters == []


def test_a_forced_launch_lists_each_unfinished_lead_with_its_state():
    from scripts.swarm.store import MASTER, AgentRecord
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick import tasks
    from tests.swarm.test_tick_master import direct

    swarm, runtime = direct(
        AgentRecord("master@a1b2c3-0002", MASTER, MASTER, state="suspect", seat="master@sw"),
        AgentRecord("master@a1b2c3-0003", MASTER, MASTER, state="retiring", seat="master@sw"),
    )
    runtime.live.clear()
    config = swarm.config("sw")
    actions = tick_master.run(
        "sw", config, swarm, tasks(), runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001)
    )

    assert actions == [
        "master down 5 minutes, forced a master launch",
        "master launch refused: unfinished lead master@a1b2c3-0002 (suspect), master@a1b2c3-0003 (retiring)",
    ]
    assert runtime.masters == []


@pytest.mark.parametrize(
    ("pending", "reason"),
    [
        ({"name": "master@a1b2c3-0003"}, "master launch refused: startup pending for master@a1b2c3-0003"),
        ({"alerted": True}, "master launch refused: startup retries exhausted, operator action is required"),
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
