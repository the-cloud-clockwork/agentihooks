"""Which builder each lane's opening prompt comes from, pinned before the seat lanes share one table."""

from typing import Any

import pytest

from scripts.swarm import prompt
from scripts.swarm.store import DELEGATE, DISPATCH, FULL, MASTER

pytestmark = pytest.mark.unit

MASTER_NAME, DISPATCHER, ENGINEER = "master@a1b2c3-0001", "dispatcher@a1b2c3-0001", "engineer@a1b2c3-0001"
MASTER_TASK: dict[str, Any] = {"id": "master", "handoff": "caps go to four"}
DISPATCH_TASK: dict[str, Any] = {"id": "dispatcher", "title": "Dispatch", "triggers": []}
TASK: dict[str, Any] = {"id": "t1", "title": "Fix the parser", "phase": "p1"}


@pytest.mark.parametrize("autonomy", [DELEGATE, FULL])
def test_a_master_prompt_comes_from_the_master_builder(autonomy: str) -> None:
    built = prompt.build("sw", "/repo", MASTER, MASTER_NAME, MASTER_TASK, autonomy=autonomy)
    assert built.startswith(
        f"You are {MASTER_NAME}, the master of swarm sw, working over the repo /repo. The operator talks to the swarm "
        "through you. You stay online for the life of the swarm; the swarm restarts you if you die.\n"
        "You troubleshoot with read only diagnostics, plan with the operator and configure the swarm, the ledger and "
        "the operator's environment with him through the agentihooks commands and tools. You never edit code or "
        "config files in a repository, commit, merge or claim a task: engineers do that.\n"
    )
    assert built == prompt.build_master("sw", "/repo", MASTER_NAME, MASTER_TASK, autonomy)


@pytest.mark.parametrize("autonomy", [DELEGATE, FULL])
def test_a_dispatcher_prompt_comes_from_the_dispatcher_builder(autonomy: str) -> None:
    built = prompt.build("sw", "/repo", DISPATCH, DISPATCHER, DISPATCH_TASK, autonomy=autonomy)
    assert built.startswith(
        f"You are {DISPATCHER}, the dispatcher of swarm sw, working beside its master in the repo /repo. The swarm "
        f"runs at {autonomy} autonomy.\n"
        "The tick woke you because its deterministic passes could not settle these triggers:\n"
    )
    assert built == prompt.build_dispatcher("sw", "/repo", DISPATCHER, DISPATCH_TASK, autonomy)


def test_a_worker_prompt_names_its_role_and_its_one_task() -> None:
    built = prompt.build("sw", "/repo", "eng", ENGINEER, TASK)
    assert built.startswith(
        f"You are {ENGINEER}, an engineer in swarm sw, working in the repo /repo.\n"
        "Your one task for this session is t1: Fix the parser\n"
    )
    named = prompt.build("sw", "/repo", "eng", ENGINEER, TASK, role="a documentation writer")
    assert named.startswith(f"You are {ENGINEER}, a documentation writer in swarm sw, working in the repo /repo.\n")


@pytest.mark.parametrize("lane", [MASTER, DISPATCH, "eng", "ci", "plan"])
def test_a_carried_opening_is_returned_as_it_is_for_every_lane(lane: str) -> None:
    assert prompt.build("sw", "/repo", lane, "name@a1b2c3-0001", {"hive_opening": "carried opening"}) == (
        "carried opening"
    )
