import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import __main__ as cli
from scripts.ci_lab import compact, contract, expand_tests, queue, scheduler
from scripts.ci_lab.objects import LocalObjects, unit_path
from scripts.ci_lab.outcomes import PASSED
from scripts.ci_lab.units import Unit
from tests.ci_lab.test_manifest_records import JOBS, TEST, legacy, packed, records, started
from tests.ci_lab.test_scheduler import BASE, HEAD_A, HEAD_B, OTHER_TREE, TREE, Run, fake_redis, key
from tests.ci_lab.test_scheduler import lab as lab


def stripped(found: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for name, value in found.items():
        if name.startswith(scheduler.GRADES):
            document = json.loads(value)
            for slot in document["units"].values():
                slot.pop("manifest", None)
            value = document
        elif name.startswith(JOBS):
            value = {field: text for field, text in value.items() if field != "manifest"}
        kept[name] = value
    return kept


def parsed(found: dict[str, Any]) -> dict[str, Any]:
    return {name: json.loads(value) if name.startswith(scheduler.GRADES) else value for name, value in found.items()}


class Counted(LocalObjects):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.calls: list[tuple[str, str]] = []

    def write_once(self, path: str, data: bytes) -> bool:
        self.calls.append(("write", path))
        return super().write_once(path, data)

    def read(self, path: str) -> bytes:
        self.calls.append(("read", path))
        return super().read(path)


def batches(*numbers: int) -> tuple[dict[str, Any], tuple[Unit, ...]]:
    tests = {
        f"tests/test_{number}.py::test_{number}": {
            "units": [],
            "files": [f"scripts/module_{number}.py"],
            "escapes": False,
        }
        for number in numbers
    }
    units = tuple(
        Unit(expand_tests.KIND, key(expand_tests.KIND, number), (test,), json.dumps({"tests": {test: record}}))
        for number, (test, record) in zip(numbers, tests.items(), strict=True)
    )
    return {"commit": BASE, "units": [], "tests": tests}, units


def opened(lab: Callable[..., Run], *grades: tuple[tuple[Unit, ...], str]) -> Run:
    heads = (HEAD_A, HEAD_B)[: len(grades)]
    plans = {
        head: scheduler.Plan(TREE, BASE, {"unit": units}, manifest=commit)
        for head, (units, commit) in zip(heads, grades, strict=True)
    }
    run = lab(plans)
    for lane, head in enumerate(heads, 1):
        run.push(head, lane=f"engineer-{lane}")
    run.tick(1000)
    return run


def embedded(run: Run, units: tuple[Unit, ...]) -> None:
    texts = {unit.key: unit.manifest for unit in units}
    for unit in units:
        run.lab.objects.remove(unit_path(unit.kind, unit.key, "manifest"))
    for name, value in records(run.lab.client).items():
        if name.startswith(scheduler.GRADES):
            document = json.loads(value)
            for slot in document["units"].values():
                slot["manifest"] = texts.get(slot["key"], "")
            run.lab.client.set(name, json.dumps(document))
        elif name.startswith(JOBS):
            run.lab.client.hset(name, "manifest", texts[value["key"]])


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_moves_each_manifest_redis_carries_to_its_unit_and_strips_it_everywhere(
    lab: Callable[..., Run],
) -> None:
    run, manifest, unit = started(lab)
    text = json.dumps({"tests": {TEST: manifest["tests"][TEST]}})
    legacy(run, unit, text)
    packed(run, manifest)
    expected = stripped(records(run.lab.client))
    objects = Counted(run.lab.objects.root)
    counts = compact.compact(run.lab.client, objects)
    assert parsed(records(run.lab.client)) == expected
    assert objects.calls == [("write", unit_path(unit.kind, unit.key, "manifest"))]
    assert run.lab.objects.read(unit_path(unit.kind, unit.key, "manifest")).decode() == text
    assert counts == Counter({"grade manifests moved": 1, "grades stripped": 1, "jobs stripped": 1})
    assert compact.compact(run.lab.client, run.lab.objects) == Counter()


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_counts_each_manifest_and_record_of_every_live_grade_and_pending_or_leased_job(
    lab: Callable[..., Run],
) -> None:
    _, first = batches(1, 2, 3)
    _, second = batches(4, 5, 6)
    run = opened(lab, (first, BASE), (second, BASE))
    assert len(run.lease(3)) == 3
    assert (run.lab.client.zcard(compact.PENDING), run.lab.client.zcard(compact.LEASED)) == (3, 3)
    embedded(run, first + second)
    expected = stripped(records(run.lab.client))
    counts = compact.compact(run.lab.client, run.lab.objects)
    assert counts == Counter({"grade manifests moved": 6, "grades stripped": 2, "jobs stripped": 6})
    assert parsed(records(run.lab.client)) == expected
    for unit in first + second:
        assert run.lab.objects.read(unit_path(unit.kind, unit.key, "manifest")).decode() == unit.manifest


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_moves_a_job_manifest_whose_grade_slot_carries_none(lab: Callable[..., Run]) -> None:
    run, manifest, unit = started(lab)
    text = json.dumps({"tests": {TEST: manifest["tests"][TEST]}})
    legacy(run, unit, text, slots=False)
    counts = compact.compact(run.lab.client, run.lab.objects)
    assert run.lab.objects.read(unit_path(unit.kind, unit.key, "manifest")).decode() == text
    assert counts == Counter({"job manifests moved": 1, "jobs stripped": 1})
    assert [
        name for name, value in records(run.lab.client).items() if name.startswith(JOBS) and "manifest" in value
    ] == []


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_slices_the_manifest_an_unfinished_test_slot_lacks_from_its_grades_packed_manifest(
    lab: Callable[..., Run],
) -> None:
    run, manifest, unit = started(lab)
    legacy(run, unit, "", slots=False, jobs=False)
    packed(run, manifest)
    counts = compact.compact(run.lab.client, run.lab.objects)
    stored = json.loads(run.lab.objects.read(unit_path(unit.kind, unit.key, "manifest")))
    assert stored == {"tests": {TEST: manifest["tests"][TEST]}}
    assert counts == Counter({"batches sliced": 1})


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_slices_the_manifest_of_a_test_slot_stored_with_an_empty_one(lab: Callable[..., Run]) -> None:
    run, manifest, unit = started(lab)
    legacy(run, unit, "", jobs=False)
    packed(run, manifest)
    counts = compact.compact(run.lab.client, run.lab.objects)
    assert counts == Counter({"grades stripped": 1, "batches sliced": 1})
    stored = json.loads(run.lab.objects.read(unit_path(unit.kind, unit.key, "manifest")))
    assert stored == {"tests": {TEST: manifest["tests"][TEST]}}


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_counts_the_batches_whose_grade_commit_has_no_packed_manifest(lab: Callable[..., Run]) -> None:
    _, first = batches(1)
    _, second = batches(2)
    run = opened(lab, (first, BASE), (second, OTHER_TREE))
    for unit in first + second:
        legacy(run, unit, "", slots=False, jobs=False)
    assert compact.compact(run.lab.client, run.lab.objects) == Counter({"batches without a packed manifest": 2})
    stored = set(run.lab.objects.keys("lab/"))
    assert [unit for unit in first + second if unit_path(unit.kind, unit.key, "manifest") in stored] == []


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_slices_nothing_for_a_grade_state_stored_before_it_kept_a_manifest_commit(
    lab: Callable[..., Run],
) -> None:
    run, _, unit = started(lab)
    legacy(run, unit, "", slots=False, jobs=False)
    (grade,) = run.grades()
    document = json.loads(run.lab.client.get(scheduler.GRADES + grade.run))
    del document["manifest"]
    run.lab.client.set(scheduler.GRADES + grade.run, json.dumps(document))
    assert compact.compact(run.lab.client, run.lab.objects) == Counter()


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_slices_no_manifest_for_a_finished_test_slot(lab: Callable[..., Run]) -> None:
    manifest, units = batches(1, 2)
    run = opened(lab, (units, BASE))
    (grant,) = run.lease(1)
    run.finish(grant, PASSED, {"seconds": 1.0})
    run.tick(2000)
    (finished,) = [unit for unit in units if unit.key == grant.unit.key]
    (waiting,) = [unit for unit in units if unit.key != grant.unit.key]
    assert run.grade(HEAD_A).units[f"{finished.kind}:{finished.key}"].outcome == PASSED
    assert run.lab.client.sismember(scheduler.LIVE, run.grade(HEAD_A).run)
    for unit in units:
        legacy(run, unit, "", slots=False, jobs=False)
    packed(run, manifest)
    assert compact.compact(run.lab.client, run.lab.objects) == Counter({"batches sliced": 1})
    assert unit_path(finished.kind, finished.key, "manifest") not in run.lab.objects.keys("lab/")
    stored = run.lab.objects.read(unit_path(waiting.kind, waiting.key, "manifest"))
    assert json.loads(stored) == json.loads(waiting.manifest)


class Racing:
    def __init__(self, client: Any, value: str) -> None:
        self.client, self.value = client, value

    def pipeline(self, *args: Any, **options: Any) -> Any:
        pipe = self.client.pipeline(*args, **options)
        watch, multi = pipe.watch, pipe.multi
        watched: list[str] = []

        def watching(*names: str) -> None:
            watched.extend(names)
            watch(*names)

        def racing() -> None:
            for name in watched:
                self.client.set(name, self.value)
            multi()

        pipe.watch, pipe.multi = watching, racing
        return pipe

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_keeps_a_grade_state_the_scheduler_wrote_meanwhile(lab: Callable[..., Run]) -> None:
    _, first = batches(1)
    _, second = batches(2)
    run = opened(lab, (first, BASE), (second, BASE))
    embedded(run, first + second)
    names = sorted(scheduler.GRADES + grade.run for grade in run.grades())
    written = json.dumps({"written": "meanwhile"})
    counts = compact.compact(Racing(run.lab.client, written), run.lab.objects)
    assert [run.lab.client.get(name) for name in names] == [written, written]
    assert counts == Counter({"grade manifests moved": 2, "grades written meanwhile": 2, "jobs stripped": 2})
    for unit in first + second:
        assert run.lab.objects.read(unit_path(unit.kind, unit.key, "manifest")).decode() == unit.manifest


@pytest.mark.feature("lab-manifest-compaction")
def test_the_compact_command_compacts_the_lab_redis_its_environment_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import redis

    client = fake_redis()
    objects = LocalObjects(tmp_path)
    environ = {contract.REDIS_URL: "redis://lab"}
    opened = {("redis://lab", True): client}
    monkeypatch.setattr(redis.Redis, "from_url", lambda url, decode_responses: opened[(url, decode_responses)])
    monkeypatch.setattr(compact, "storage", lambda found: objects if found is environ else None)
    unit_key = key("test", 1)
    client.hset(JOBS + "job", mapping={"kind": "test", "key": unit_key, "manifest": '{"tests": {}}'})
    client.zadd(queue.PREFIX + "pending", {"job": 1})
    client.sadd(scheduler.LIVE, "gone")
    assert cli.main(["compact"], environ) == 0
    assert json.loads(capsys.readouterr().out) == {"job manifests moved": 1, "jobs stripped": 1}
    assert objects.read(unit_path("test", unit_key, "manifest")) == b'{"tests": {}}'
    assert client.hgetall(JOBS + "job") == {"kind": "test", "key": unit_key}


@pytest.mark.feature("lab-manifest-compaction")
def test_the_compact_command_help_names_its_invocation(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["compact", "--help"], {})
    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith("usage: python -m scripts.ci_lab compact")
