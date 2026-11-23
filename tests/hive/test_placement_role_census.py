import json
from types import SimpleNamespace

import pytest

from scripts.hive import headroom, launch, placement, registry
from scripts.swarm import commands, local_slots
from scripts.swarm.capacity import Account
from scripts.swarm.profile_choice import ProfileDecision
from scripts.swarm.store import AgentRecord
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive


@pytest.fixture
def census(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    monkeypatch.setattr(local_slots, "occupancy", lambda store, hive: (0, 0))
    monkeypatch.setattr(local_slots, "held", lambda store, hive, slug, task: 0)
    monkeypatch.setattr(local_slots, "maximum", lambda store, slug, cap: cap)
    setup.store.redis.srem(registry.INDEX, "remote")
    setup.hive["id"] = "workstation"
    write_hive(setup, max_agents=12, ui="yes", version="2.17.1181")
    setup.herdr._quota_accounts = [Account("claude", "account", "room", 0, 80, 80, 100)]
    setup.herdr._profile_decision.return_value = ProfileDecision("expert", "task", "expert")
    return setup


def _start(census: SimpleNamespace, lane: str, index: int, hive: str = "workstation") -> AgentRecord:
    return census.store.start_execution(
        census.config.slug,
        AgentRecord(
            census.store.next_name(census.config.slug, lane),
            lane,
            "task",
            seat=f"{lane}-{index}@{census.config.slug}",
            hive=hive,
            runtime_target={"host_id": "workstation"},
            harness="claude",
            account="account",
        ),
    )


def _report(census: SimpleNamespace, total: int, executions: list[str], markers: int = 0) -> None:
    census.store.redis.set(
        f"{registry.key('workstation')}:sessions",
        json.dumps(
            {
                "claude:account": total,
                "interactive:claude:account": markers,
                "_executions": {"claude:account": executions},
            }
        ),
    )


@pytest.mark.feature("placement-role-census")
@pytest.mark.parametrize("markers", [0, 1, 3], ids=["subscription", "mixed", "interactive"])
def test_three_masters_and_eleven_workers_leave_the_kept_expert_room_usable(
    census: SimpleNamespace, markers: int
) -> None:
    masters = [_start(census, "master", index, "" if index == 0 else "workstation") for index in range(3)]
    workers = [_start(census, "eng", index) for index in range(11)]
    _report(census, 14, [agent.execution_id for agent in masters + workers], markers)

    hive = launch.loaded_hives(census.store)[0]
    assert (hive["agents"], hive["room"]) == (11, 1)
    selected = census.launcher.select(census.config, "expert", {"id": "expert-successor"})
    assert isinstance(selected, launch.Selection)
    assert selected.hive == "workstation"


@pytest.mark.feature("placement-role-census")
def test_a_master_execution_does_not_prevent_unclaimed_processes_from_absorbing_pending_workers(
    census: SimpleNamespace,
) -> None:
    master = _start(census, "master", 1)
    _start(census, "eng", 1)
    _start(census, "eng", 2)
    _report(census, 2, [master.execution_id])

    assert launch.loaded_hives(census.store)[0]["agents"] == 2


@pytest.mark.feature("placement-role-census")
def test_unreported_masters_never_erase_an_unregistered_worker_process(census: SimpleNamespace) -> None:
    master = _start(census, "master", 1)
    _start(census, "master", 2)
    _start(census, "master", 3, "other")
    _report(census, 2, [master.execution_id, master.execution_id])

    assert launch.loaded_hives(census.store)[0]["agents"] == 1


@pytest.mark.feature("placement-role-census")
def test_interactive_markers_still_exempt_a_master_whose_execution_is_unreported(census: SimpleNamespace) -> None:
    _report(census, 2, [], markers=1)

    assert launch.loaded_hives(census.store)[0]["agents"] == 1


@pytest.mark.feature("placement-role-census")
def test_master_exclusion_does_not_absorb_workers_from_another_account_slot(census: SimpleNamespace) -> None:
    master = _start(census, "master", 1)
    _start(census, "eng", 1)
    _start(census, "eng", 2)
    census.store.redis.set(
        f"{registry.key('workstation')}:sessions",
        json.dumps(
            {
                "claude:account": 1,
                "codex:other": 2,
                "_executions": {"claude:account": [master.execution_id]},
            }
        ),
    )

    assert launch.loaded_hives(census.store)[0]["agents"] == 4


@pytest.mark.feature("placement-role-census")
def test_excluding_masters_keeps_a_saturated_cpu_refusal(census: SimpleNamespace) -> None:
    master = _start(census, "master", 1)
    _report(census, 2, [master.execution_id])
    reading = json.loads(census.store.redis.get(headroom.key("workstation")))
    reading.update(cpus=1, load5=10.0)
    census.store.redis.set(headroom.key("workstation"), json.dumps(reading))

    hive = launch.loaded_hives(census.store)[0]
    assert hive["room"] == 0
    assert "cpu" in hive["refusal"].lower()
    selected = census.launcher.select(census.config, "expert", {"id": "expert-successor"})
    assert isinstance(selected, placement.Wait)
