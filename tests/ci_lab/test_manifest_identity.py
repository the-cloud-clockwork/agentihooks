import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Thread
from typing import cast

import pytest

from scripts.ci_lab import executors, expand_tests, lease_api, queue, run_tests, scheduler, worker
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.objects import unit_path
from scripts.ci_lab.outcomes import PASSED
from scripts.ci_lab.units import Unit
from tests.ci_lab import test_executors as support
from tests.ci_lab.test_scheduler import BASE, HEAD_A, HEAD_B, TREE, Run, key
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_worker import Signer


def batch(unlisted: bool) -> Unit:
    path = "tests/test_a.py"
    manifest = {
        "units": [],
        "tests": {} if unlisted else {f"{path}::test_a": {"files": ["scripts/a.py"], "outside": [], "units": []}},
    }
    tree = {path: b"def test_a():\n    assert True\n", "scripts/a.py": b"value = 1\n"}
    toolchain = Toolchain("3.12", {}, "lock", "project", "image", {})
    units = (
        expand_tests.unlisted_units(manifest, tree, [path], toolchain)
        if unlisted
        else expand_tests.units(manifest, tree, {}, {}, toolchain)
    )
    return units[0]


def leased(run: Run) -> worker.Lease:
    server = lease_api.serve("127.0.0.1", 0, run.leases(), Signer("http://unused"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = worker.Client(f"http://127.0.0.1:{server.server_address[1]}")
        leases = client.lease("worker", 1, frozenset(), 0)
        assert len(leases) == 1
        return leases[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.feature("lab-test-manifest-identity")
@pytest.mark.parametrize("unlisted", [False, True])
def test_expansion_records_the_identity_of_its_exact_batch_bytes(unlisted: bool) -> None:
    unit = batch(unlisted)
    assert unit.manifest_digest == hashlib.sha256(unit.manifest.encode()).hexdigest()
    assert unit == replace(unit, manifest="other", manifest_digest="other")


@pytest.mark.feature("lab-test-manifest-identity")
@pytest.mark.parametrize("unlisted", [False, True])
@pytest.mark.parametrize("plant", ["swapped", "whitespace"])
def test_a_swapped_stored_manifest_is_refused_and_the_next_unit_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab: Callable[..., Run], unlisted: bool, plant: str
) -> None:
    original = batch(unlisted)
    expected = hashlib.sha256(original.manifest.encode()).hexdigest()
    unit = replace(original, manifest_digest=expected)
    second = replace(unit, key=key("test", 2))
    plans = {
        HEAD_A: scheduler.Plan(TREE, BASE, {"unit": (unit,)}),
        HEAD_B: scheduler.Plan(TREE, BASE, {"unit": (second,)}),
    }
    run = lab(plans)
    run.push(HEAD_A, lane="first")
    with monkeypatch.context() as held:
        held.setattr(run.engine, "_admit", lambda _now: [])
        run.tick(1000)
    old = run.engine
    run.engine = scheduler.Scheduler(run.lab.client, run.lab.objects, old.ports, old.settings)
    run.tick(1001)
    path = unit_path(unit.kind, unit.key, "manifest")
    swapped = '{"tests": {"another::test": {}}}' if plant == "swapped" else unit.manifest + "\n "
    assert (
        json.loads(swapped) != json.loads(unit.manifest)
        if plant == "swapped"
        else json.loads(swapped) == json.loads(unit.manifest)
    )
    run.lab.objects.remove(path)
    run.lab.objects.write_once(path, swapped.encode())
    first = leased(run)
    assert first.unit.manifest == swapped
    assert first.unit.manifest_digest == expected
    pod = support.pod_of(tmp_path / "worker", support.FakeWorktrees({}))
    suite = executors.Suite(pod.root / "manifest.json", pod.root / "flakes.jsonl")
    venv = support.Recorder((tmp_path / "venv", False))
    ran = support.Recorder(run_tests.Verdict(PASSED, False, (), {}, 1, tmp_path / "log", tmp_path / "coverage"))
    monkeypatch.setattr(executors.venvs, "venv_for", venv)
    monkeypatch.setattr(executors.run_tests, "run", ran)
    with pytest.raises(ValueError, match="manifest digest mismatch") as refused:
        executors.tests(pod, suite, first)
    assert type(refused.value).__name__ == "ManifestRefused"
    assert first.unit.key in str(refused.value)
    assert cast(support.FakeWorktrees, pod.trees.worktrees).calls == []
    assert venv.calls == []
    assert ran.calls == []
    run.push(HEAD_B, lane="second")
    run.tick(1002)
    following = leased(run)
    assert following.head == HEAD_B
    assert following.unit.manifest_digest == expected
    tree = support.folder(tmp_path, "tree", {executors.venvs.LOCKFILE: "lock"})
    healthy = support.pod_of(tmp_path / "healthy", support.FakeWorktrees({HEAD_B: tree}))
    assert executors.tests(healthy, suite, following).outcome == PASSED
    assert len(ran.calls) == 1


@pytest.mark.feature("lab-test-manifest-identity")
@pytest.mark.parametrize("missing", ["digest", "manifest", "mismatch"])
def test_manifest_refusal_precedes_all_test_execution_resources(tmp_path: Path, missing: str) -> None:
    manifest = '{"tests": {}}'
    digest = hashlib.sha256(manifest.encode()).hexdigest()
    unit = Unit(
        "test",
        "key",
        ("tests/test_a.py",),
        "" if missing == "manifest" else manifest,
        "" if missing == "digest" else "wrong" if missing == "mismatch" else digest,
    )
    lease = worker.Lease("job", "token", HEAD_A, unit, 1, 1500, BASE, BASE)
    pod = support.pod_of(tmp_path, support.FakeWorktrees({}))
    suite = executors.Suite(tmp_path / "absent", tmp_path / "flakes")
    reason = {
        "digest": "missing manifest digest",
        "manifest": "missing manifest",
        "mismatch": "manifest digest mismatch",
    }[missing]
    with pytest.raises(ValueError, match=reason) as refused:
        executors.tests(pod, suite, lease)
    assert type(refused.value).__name__ == "ManifestRefused"
    assert unit.key in str(refused.value)
    assert cast(support.FakeWorktrees, pod.trees.worktrees).calls == []


@pytest.mark.feature("lab-test-manifest-identity")
@pytest.mark.parametrize("old", ["grade", "queue"])
def test_old_records_without_identity_are_parsed_then_refused_after_claiming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab: Callable[..., Run], old: str
) -> None:
    original = batch(False)
    unit = replace(original, manifest_digest=hashlib.sha256(original.manifest.encode()).hexdigest())
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"unit": (unit,)})})
    run.push(HEAD_A)
    with monkeypatch.context() as held:
        held.setattr(run.engine, "_admit", lambda _now: [])
        run.tick(1000)
    if old == "grade":
        for state in run.grades():
            key = scheduler.GRADES + state.run
            raw = json.loads(run.lab.client.get(key))
            for slot in raw["units"].values():
                slot.pop("manifest_digest", None)
            run.lab.client.set(key, json.dumps(raw))
        previous = run.engine
        run.engine = scheduler.Scheduler(run.lab.client, run.lab.objects, previous.ports, previous.settings)
    run.tick(1001)
    if old == "queue":
        for state in run.grades():
            for slot in state.units.values():
                run.lab.client.hdel(queue._job(slot.job), "manifest_digest")
    lease = leased(run)
    decoded = run.engine.queue.job(lease.job)
    assert decoded is not None
    assert decoded.unit.manifest_digest == ""
    assert lease.unit.manifest_digest == ""
    pod = support.pod_of(tmp_path, support.FakeWorktrees({}))
    suite = executors.Suite(tmp_path / "absent", tmp_path / "flakes")
    with pytest.raises(ValueError, match="missing manifest digest"):
        executors.tests(pod, suite, lease)
    assert cast(support.FakeWorktrees, pod.trees.worktrees).calls == []
