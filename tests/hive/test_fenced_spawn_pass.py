"""Fenced spawns in one placement pass share one process scan and one host reading, and time their parts."""

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

    def call(*args: Any) -> Any:
        calls.append(args)
        return value

    monkeypatch.setattr(module, name, call)
    return calls


def local(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> tuple[FencedRuntime, list, list]:
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    setup.store.update(setup.config.slug, local_max_agents=LAUNCHES)
    scans = counted(monkeypatch, terminate_agent, "sessions", [])
    readings = counted(monkeypatch, headroom, "measure", READING)
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


def test_concurrent_launches_in_one_pass_scan_processes_and_read_the_host_once(setup, monkeypatch):
    fenced, scans, readings = local(setup, monkeypatch)
    fenced.begin_placement_pass()
    refusals = refuse_together(fenced, 0)
    fenced.end_placement_pass()
    assert refusals == dict.fromkeys(range(LAUNCHES), "")
    assert len(setup.store.redis.hgetall(local_slots.key("workstation"))) == LAUNCHES
    assert (len(scans), len(readings)) == (1, 1)


def test_the_next_pass_scans_again_and_a_launch_outside_a_pass_scans_each_time(setup, monkeypatch):
    fenced, scans, readings = local(setup, monkeypatch)
    for _ in range(2):
        fenced.begin_placement_pass()
        assert fenced._refusal("eng", {"id": "task"}, "") == ""
        assert fenced._refusal("eng", {"id": "other"}, "") == ""
        fenced.end_placement_pass()
    assert (len(scans), len(readings)) == (2, 2)
    assert fenced._refusal("eng", {"id": "task"}, "") == ""
    assert (len(scans), len(readings)) == (4, 3)


def test_a_pass_cache_reads_each_value_once_and_hands_it_to_every_caller() -> None:
    cache, reads = local_slots.PassCache(), []

    def read() -> set[str]:
        reads.append(1)
        return {"engineer@aaaaaa-0001"}

    assert cache.once("live", read) == {"engineer@aaaaaa-0001"}
    assert cache.once("live", read) is cache.once("live", read)
    assert cache.once("other", lambda: 0) == 0
    assert len(reads) == 1


def test_a_claim_in_a_pass_counts_live_workers_from_the_pass_scan(setup, monkeypatch):
    sessions = [SimpleNamespace(name="engineer@bbbbbb-0001", target="claude", status="alive")]
    scans = counted(monkeypatch, terminate_agent, "sessions", sessions)
    cache = local_slots.PassCache()
    assert local_slots.running(setup.store, setup.config.slug, "task", cache) == 1
    assert local_slots.claim(setup.store, "workstation", setup.config.slug, "task", "new", 1, cache) == 1
    assert local_slots.claim(setup.store, "workstation", setup.config.slug, "task", "new", 2, cache) == 1
    assert setup.store.redis.hgetall(local_slots.key("workstation")).keys() == {f"{setup.config.slug}/new"}
    assert len(scans) == 1


def test_a_fenced_spawn_times_its_placement_and_its_runtime_launch(setup, monkeypatch):
    fenced, _, _ = local(setup, monkeypatch)
    steps: list[str] = []
    original = timing.step

    def step(name: str, report: bool = True) -> Any:
        steps.append(name)
        return original(name, report)

    monkeypatch.setattr(timing, "step", step)
    assert fenced.spawn(setup.config, "eng", "engineer-0", {"id": "task"}) == setup.herdr.spawn.return_value
    assert [name for name in steps if name.startswith("scripts.swarm.controller.")] == [
        "scripts.swarm.controller.FencedRuntime._refusal",
        "scripts.swarm.controller.FencedRuntime.runtime_spawn",
    ]
