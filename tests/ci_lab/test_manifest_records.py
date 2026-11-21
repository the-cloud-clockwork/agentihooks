import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts import dep_manifest
from scripts.ci_lab import expand_tests, leases, queue, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.objects import ROOT, LocalObjects, manifest_folder, unit_path
from scripts.ci_lab.units import Unit
from tests.ci_lab.test_scheduler import BASE, HEAD_A, SLUG, TREE, Run, key
from tests.ci_lab.test_scheduler import lab as lab

FILES = tuple(f"scripts/module_{index}.py" for index in range(400))
TEST = "tests/test_records.py::test_records"
JOBS = queue.PREFIX + "job:"


def suite() -> tuple[dict[str, Any], tuple[Unit, ...]]:
    record = {"units": [], "files": list(FILES), "escapes": False}
    manifest = {"commit": BASE, "units": [], "tests": {TEST: record}}
    tree = {
        "tests/test_records.py": b"def test_records():\n    assert True\n",
        **{path: b"value = 1\n" for path in FILES},
    }
    toolchain = Toolchain("3.12", {}, "lock", "project", "image", {})
    return manifest, expand_tests.units(manifest, tree, {}, {}, toolchain)


def started(lab: Callable[..., Run]) -> tuple[Run, dict[str, Any], Unit]:
    manifest, units = suite()
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"unit": units}, manifest=BASE)})
    run.push(HEAD_A)
    run.tick(1000)
    (unit,) = units
    return run, manifest, unit


def records(client: Any) -> dict[str, Any]:
    reads: dict[str, Callable[[str], Any]] = {
        "string": client.get,
        "hash": client.hgetall,
        "set": client.smembers,
        "zset": lambda key: client.zrange(key, 0, -1, withscores=True),
        "list": lambda key: client.lrange(key, 0, -1),
    }
    return {name: reads[client.type(name)](name) for name in client.keys("*")}


def packed(run: Run, manifest: dict[str, Any]) -> None:
    path = f"{manifest_folder(SLUG)}{BASE}.json"
    run.lab.objects.write_once(path, json.dumps(dep_manifest.pack(copy.deepcopy(manifest))).encode())


def legacy(run: Run, unit: Unit, text: str, slots: bool = True, jobs: bool = True) -> None:
    run.lab.objects.remove(unit_path(unit.kind, unit.key, "manifest"))
    for name, value in records(run.lab.client).items():
        if slots and name.startswith(scheduler.GRADES):
            document = json.loads(value)
            for slot in document["units"].values():
                slot["manifest"] = text if slot["kind"] == unit.kind else ""
            run.lab.client.set(name, json.dumps(document))
        elif jobs and name.startswith(JOBS):
            run.lab.client.hset(name, "manifest", text)


@pytest.mark.feature("lab-manifest-records-bounded")
def test_a_started_grade_keeps_its_batch_manifest_out_of_redis_and_its_lease_carries_it(
    lab: Callable[..., Run],
) -> None:
    run, manifest, _ = started(lab)
    stored = records(run.lab.client)
    assert sum(name.startswith(JOBS) for name in stored) == 1
    assert [name for name, value in stored.items() if FILES[-1] in str(value)] == []
    (grant,) = run.lease(2)
    assert json.loads(grant.unit.manifest) == {"tests": {TEST: manifest["tests"][TEST]}}


@pytest.mark.feature("lab-manifest-records-bounded")
def test_a_scheduler_start_rewrites_no_stored_grade_or_job(lab: Callable[..., Run]) -> None:
    run, manifest, _ = started(lab)
    for name, value in records(run.lab.client).items():
        if name.startswith(scheduler.GRADES):
            document = json.loads(value)
            for slot in document["units"].values():
                slot.pop("manifest", None)
            run.lab.client.set(name, json.dumps(document))
        elif name.startswith(JOBS):
            run.lab.client.hdel(name, "manifest")
    packed(run, manifest)
    before = records(run.lab.client)
    scheduler.Scheduler(run.lab.client, run.lab.objects, run.engine.ports, run.engine.settings)
    assert records(run.lab.client) == before


@pytest.mark.feature("lab-manifest-records-bounded")
def test_a_grade_state_stored_with_slot_manifests_reads_and_saves_without_them(lab: Callable[..., Run]) -> None:
    run, _, _ = started(lab)
    (grade,) = run.grades()
    name = scheduler.GRADES + grade.run
    document = json.loads(run.lab.client.get(name))
    for slot in document["units"].values():
        slot["manifest"] = FILES[-1]
    run.lab.client.set(name, json.dumps(document))
    state = run.engine.grade(grade.run)
    assert state is not None
    run.engine.save(state)
    assert FILES[-1] not in run.lab.client.get(name)


@pytest.mark.feature("lab-manifest-records-bounded")
def test_a_lease_reads_the_stored_manifest_of_a_test_unit_only(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path)
    test = Unit("test", key("test", 1), ("tests/test_a.py::test_a",), '{"tests": {}}')
    static = Unit("static", key("static", 2), ("lint",), "static text")
    bare = Unit("test", key("test", 3), ("tests/test_b.py::test_b",))
    assert scheduler.keep_manifests(objects, [test, static, bare]) == 2
    assert scheduler.keep_manifests(objects, [test]) == 0
    assert objects.keys(ROOT) == sorted(unit_path(unit.kind, unit.key, "manifest") for unit in (test, static))
    assert [scheduler.stored_manifest(objects, unit) for unit in (test, static, bare)] == ['{"tests": {}}', "", ""]
    assert leases.unstored(test) == ""
