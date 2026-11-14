"""Concurrent fenced launches stop at the local cap, a local slot pass cache reads each value once, and a fenced
spawn times its parts."""

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import terminate_agent
from scripts.hive import headroom
from scripts.swarm import commands, local_slots, timing
from scripts.swarm.controller import FencedRuntime
from tests.hive.test_controller_launch import setup as setup

LAUNCHES = 5
READING = {"cpus": 64, "load5": 0.0, "available_mb": 10**6, "total_mb": 10**6}


def counted(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, value: Any) -> list[tuple]:
    calls: list[tuple] = []

    def call(*args: Any, **kwargs: Any) -> Any:
        calls.append((*args, kwargs))
        return value

    monkeypatch.setattr(module, name, call)
    return calls


def local(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> tuple[FencedRuntime, list, list]:
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    setup.store.update(setup.config.slug, local_max_agents=LAUNCHES)
    scans = counted(monkeypatch, terminate_agent, "sessions", [])
    readings = counted(monkeypatch, headroom, "measure", READING)
    monkeypatch.setattr(headroom, "read", lambda redis, machine, now: {"saved": machine})
    return FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local"), scans, readings


def refuse_together(fenced: FencedRuntime, first: int) -> dict[int, str]:
    barrier, refusals = threading.Barrier(LAUNCHES), {}

    def refuse(index: int) -> None:
        barrier.wait()
        refusals[index] = fenced._refusal("eng", {"id": f"task-{index}"}, f"engineer-{index}")

    threads = [threading.Thread(target=refuse, args=(index,)) for index in range(first, first + LAUNCHES)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return refusals


def test_concurrent_launches_stop_at_the_local_cap(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    fenced, _, _ = local(setup, monkeypatch)
    setup.store.update(setup.config.slug, local_max_agents=LAUNCHES - 2)
    refusals = refuse_together(fenced, 0)
    rechecked = fenced.placement_refusal("eng", {"id": "task-9"})
    assert "local machine runs 3 of its 3 agents" in rechecked
    admitted = [index for index, refused in refusals.items() if not refused]
    full = [refused for refused in refusals.values() if "local machine runs 3 of its 3 agents" in refused]
    assert (len(admitted), len(full)) == (LAUNCHES - 2, 2)
    assert len(setup.store.redis.hgetall(local_slots.key("workstation"))) == LAUNCHES - 2


def test_a_pass_cache_reads_each_value_once_and_hands_it_to_every_caller() -> None:
    cache, reads = local_slots.PassCache(), []

    def read() -> set[str]:
        reads.append(1)
        return {"engineer@aaaaaa-0001"}

    assert cache.once(read) == {"engineer@aaaaaa-0001"}
    assert cache.once(read) is cache.once(read)
    assert cache.once(len, "one") == 3
    assert cache.once(len, "three") == 5
    assert len(reads) == 1


def test_a_claim_in_a_pass_counts_live_workers_from_the_pass_scan(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = [SimpleNamespace(name="engineer@bbbbbb-0001", target="claude", status="alive")]
    scans = counted(monkeypatch, terminate_agent, "sessions", sessions)
    cache = local_slots.PassCache()
    assert local_slots.running(setup.store, setup.config.slug, "task", cache) == 1
    assert local_slots.claim(setup.store, "workstation", setup.config.slug, "task", "new", 1, cache) == 1
    assert local_slots.claim(setup.store, "workstation", setup.config.slug, "task", "new", 2, cache) == 1
    assert setup.store.redis.hgetall(local_slots.key("workstation")).keys() == {f"{setup.config.slug}/new"}
    assert len(scans) == 1


def test_a_fenced_spawn_times_its_placement_and_its_runtime_launch(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    fenced, _, _ = local(setup, monkeypatch)
    steps: list[tuple[str, bool]] = []
    original = timing.step

    def step(name: str, report: bool = True) -> Any:
        steps.append((name, report))
        return original(name, report)

    monkeypatch.setattr(timing, "step", step)
    assert fenced.spawn(setup.config, "eng", "engineer-0", {"id": "task"}) == setup.herdr.spawn.return_value
    assert [(name, report) for name, report in steps if name.startswith("scripts.swarm.controller.")] == [
        ("scripts.swarm.controller.FencedRuntime._refusal", False),
        ("scripts.swarm.controller.FencedRuntime.runtime_spawn", False),
    ]
