from dataclasses import replace

import pytest

from tests.hive.test_controller_launch import setup, write_hive  # noqa: F401


def selected(world, config, count):
    world.launcher.placement_cache = {}
    return [world.launcher.select(config, "eng", {"id": f"task-{index}"}).hive for index in range(count)]


@pytest.mark.parametrize(
    ("nodes", "expected"),
    [
        (2, ["a", "b", "a", "b", "a", "b"]),
        (0, ["a", "b", "c", "remote", "a", "b"]),
    ],
)
def test_one_placement_pass_spreads_agents_over_the_chosen_hives(setup, nodes, expected):  # noqa: F811
    for name in ("c", "b", "a"):
        write_hive(setup, id=name)
    config = replace(setup.config, placement_mode="round-robin", placement_nodes=nodes)

    assert selected(setup, config, 6) == expected


def test_one_placement_pass_counts_each_placed_agent_once(setup):  # noqa: F811
    from scripts.hive import placement

    for name in ("b", "a"):
        write_hive(setup, id=name)
    config = replace(setup.config, placement_mode="round-robin", placement_nodes=0)

    assert selected(setup, config, 4) == ["a", "b", "remote", "a"]
    assert {hive["id"]: hive.get(placement.PLACED) for hive in setup.launcher.hives()} == {
        "a": 2,
        "b": 1,
        "remote": 1,
    }


def test_round_robin_counts_only_hives_that_list_the_swarm_repo(setup):  # noqa: F811
    write_hive(setup, id="a", repos=["other"])
    write_hive(setup, id="b")
    config = replace(setup.config, placement_mode="round-robin", placement_nodes=2)

    assert selected(setup, config, 4) == ["b", "remote", "b", "remote"]


def test_anton_first_keeps_the_ranked_hive_while_it_has_room(setup):  # noqa: F811
    write_hive(setup, id="a")
    write_hive(setup, id="ranked", prefer={"eng": 1})

    assert selected(setup, setup.config, 4) == ["ranked", "ranked", "ranked", "a"]


def test_anton_first_fills_the_lowest_id_among_equal_hives_in_one_pass(setup):  # noqa: F811
    write_hive(setup, id="a")

    assert selected(setup, setup.config, 4) == ["a", "a", "a", "remote"]


def test_round_robin_leaves_ui_roles_on_their_ranked_hive(setup):  # noqa: F811
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("qa", "classifier", "qa")
    write_hive(setup, id="a", ui="yes")
    write_hive(setup, id="ranked", ui="yes", prefer={"qa": 1})
    config = replace(setup.config, placement_mode="round-robin", placement_nodes=1)

    assert setup.launcher.select(config, "eng", {"id": "task"}).hive == "ranked"


def test_round_robin_keeps_a_task_pinned_to_a_hive_outside_the_node_limit(setup):  # noqa: F811
    write_hive(setup, id="a")
    write_hive(setup, id="b")
    config = replace(setup.config, placement_mode="round-robin", placement_nodes=2)

    assert setup.launcher.select(config, "eng", {"id": "task", "hive": "remote"}).hive == "remote"


def test_round_robin_keeps_a_workstation_task_outside_the_node_limit(setup, monkeypatch):  # noqa: F811
    from scripts.swarm import commands

    monkeypatch.setattr(commands, "hive_id", lambda: "z")
    for name in ("a", "b", "z"):
        write_hive(setup, id=name)
    config = replace(setup.config, placement_mode="round-robin", placement_nodes=2)

    assert setup.launcher.select(config, "eng", {"id": "task", "workstation": True}).hive == "z"
