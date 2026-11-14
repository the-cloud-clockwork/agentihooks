import pytest

from scripts.swarm.store import SwarmConfig
from scripts.swarm_v2.runtime import codex


@pytest.mark.parametrize(
    "task,expected",
    [
        ({"seat": "eng-1@sw"}, []),
        ({"seat": "eng-1@sw", "previous_execution_id": "exe-old"}, ["--previous-execution", "exe-old"]),
        (
            {"seat": "eng-1@sw", "previous_execution_id": "exe-old", "admitted_execution_id": "exe-hive"},
            ["--execution", "exe-hive"],
        ),
    ],
)
def test_a_hive_admitted_execution_replaces_the_predecessor_in_the_launch_arguments(task, expected):
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0, codex_inbox_transport="app_server")
    assert codex.arguments(config, "codex", task) == [
        "--codex-transport",
        "app_server",
        "--seat",
        "eng-1@sw",
        *expected,
    ]
