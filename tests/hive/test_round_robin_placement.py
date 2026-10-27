from dataclasses import replace

import pytest

from tests.hive.test_controller_launch import setup, write_hive  # noqa: F401


@pytest.mark.parametrize(
    ("mode", "nodes", "expected"),
    [
        ("round-robin", 2, ["a", "b", "a", "b", "a", "b"]),
        ("round-robin", 0, ["a", "b", "c", "remote", "a", "b"]),
    ],
)
def test_one_placement_pass_spreads_agents_over_the_chosen_hives(setup, mode, nodes, expected):  # noqa: F811
    for name in ("c", "b", "a"):
        write_hive(setup, id=name)
    config = replace(setup.config, placement_mode=mode, placement_nodes=nodes)
    setup.launcher.placement_cache = {}

    chosen = [setup.launcher.select(config, "eng", {"id": f"task-{index}"}).hive for index in range(6)]

    assert chosen == expected


def test_anton_first_keeps_the_ranked_hive_while_it_has_room(setup):  # noqa: F811
    write_hive(setup, id="a")
    write_hive(setup, id="ranked", prefer={"eng": 1})
    setup.launcher.placement_cache = {}

    chosen = [setup.launcher.select(setup.config, "eng", {"id": f"task-{index}"}).hive for index in range(4)]

    assert chosen == ["ranked", "ranked", "ranked", "a"]
