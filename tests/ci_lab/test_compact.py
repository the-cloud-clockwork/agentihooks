import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import __main__ as cli
from scripts.ci_lab import compact, contract, queue, scheduler
from scripts.ci_lab.objects import LocalObjects, unit_path
from scripts.ci_lab.outcomes import PASSED
from tests.ci_lab.test_manifest_records import JOBS, TEST, legacy, packed, records, started
from tests.ci_lab.test_scheduler import Run, fake_redis, key
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
def test_compaction_counts_the_batches_whose_grade_commit_has_no_packed_manifest(lab: Callable[..., Run]) -> None:
    run, _, unit = started(lab)
    legacy(run, unit, "", slots=False, jobs=False)
    assert compact.compact(run.lab.client, run.lab.objects) == Counter({"batches without a packed manifest": 1})
    assert unit_path(unit.kind, unit.key, "manifest") not in run.lab.objects.keys("lab/")


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
    run, manifest, unit = started(lab)
    (grant,) = run.lease(2)
    run.finish(grant, PASSED, {"seconds": 1.0})
    run.tick(2000)
    assert run.grade(grant.head).units[f"{unit.kind}:{unit.key}"].outcome == PASSED
    legacy(run, unit, "", slots=False, jobs=False)
    packed(run, manifest)
    assert compact.compact(run.lab.client, run.lab.objects) == Counter()
    assert unit_path(unit.kind, unit.key, "manifest") not in run.lab.objects.keys("lab/")


class Racing:
    def __init__(self, client: Any, name: str, value: str) -> None:
        self.client, self.name, self.value = client, name, value

    def pipeline(self, *args: Any, **options: Any) -> Any:
        pipe = self.client.pipeline(*args, **options)
        multi = pipe.multi

        def racing() -> None:
            self.client.set(self.name, self.value)
            multi()

        pipe.multi = racing
        return pipe

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


@pytest.mark.feature("lab-manifest-compaction")
def test_compaction_keeps_a_grade_state_the_scheduler_wrote_meanwhile(lab: Callable[..., Run]) -> None:
    run, manifest, unit = started(lab)
    text = json.dumps({"tests": {TEST: manifest["tests"][TEST]}})
    legacy(run, unit, text)
    (grade,) = run.grades()
    written = json.dumps({"written": "meanwhile"})
    counts = compact.compact(Racing(run.lab.client, scheduler.GRADES + grade.run, written), run.lab.objects)
    assert run.lab.client.get(scheduler.GRADES + grade.run) == written
    assert counts == Counter({"grade manifests moved": 1, "grades written meanwhile": 1, "jobs stripped": 1})
    assert run.lab.objects.read(unit_path(unit.kind, unit.key, "manifest")).decode() == text


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
