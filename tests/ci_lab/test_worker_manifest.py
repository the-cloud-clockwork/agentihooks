import copy
import json
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import Any, cast

import pytest

from scripts import dep_manifest
from scripts.ci_lab import executors, expand_tests, lease_api, queue, run_tests, scheduler, worker
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.objects import manifest_folder
from scripts.ci_lab.outcomes import PASSED
from tests.ci_lab import test_executors as support
from tests.ci_lab.test_scheduler import BASE, HEAD_A, HEAD_B, TREE, Run
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_worker import Signer


@pytest.mark.feature("lab-worker-manifest")
@pytest.mark.parametrize("legacy", ["fresh", "queued", "held", "payload"])
@pytest.mark.parametrize("unlisted", [False, True])
def test_a_leased_batch_carries_its_manifest_to_an_empty_worker_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lab: Callable[..., Run], unlisted: bool, legacy: str
) -> None:
    toolchain = Toolchain("3.12", {}, "lock", "project", "image", {})
    plans = {}
    manifests = {}
    expected = {}
    trees = {}
    for head, name in ((HEAD_A, "a"), (HEAD_B, "b")):
        path = f"tests/test_{name}.py"
        test = f"{path}::test_{name}"
        record = {"units": [], "files": [f"scripts/{name}.py"], "outside": [f"docs/{name}.md"], "escapes": False}
        manifest = {"commit": head, "units": [], "tests": {} if unlisted else {test: record}}
        manifests[head] = manifest
        tree = {path: f"def test_{name}():\n    assert True\n".encode(), f"scripts/{name}.py": b"value = 1\n"}
        units = (
            expand_tests.unlisted_units(manifest, tree, [path], toolchain)
            if unlisted
            else expand_tests.units(manifest, tree, {}, {}, toolchain)
        )
        plans[head] = scheduler.Plan(TREE, BASE, {"unit": units}, manifest=head)
        expected[head] = {"tests": {} if unlisted else {test: record}}
        trees[head] = support.folder(tmp_path, name, {executors.venvs.LOCKFILE: "lock", path: tree[path].decode()})
    run = lab(plans)
    for head in plans:
        run.push(head, lane=head)
    if legacy == "held":
        monkeypatch.setattr(run.engine, "_admit", lambda _now: [])
    run.tick(1000)
    if legacy != "fresh":
        restore_old_schema(run, manifests, keep_jobs=legacy == "payload")
        run.tick(1001)
    routes = lease_api.ControlRoutes(run.lab.client, "", lambda: 0)
    server = lease_api.serve("127.0.0.1", 0, run.leases(), Signer("http://unused"), routes)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = worker.Client(f"http://127.0.0.1:{server.server_address[1]}")
        leases = client.lease("worker", 2, frozenset(), 0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert {lease.head for lease in leases} == {HEAD_A, HEAD_B}
    monkeypatch.setattr(executors.venvs, "venv_for", support.Recorder((tmp_path / "venv", False)))
    verdict = run_tests.Verdict(PASSED, False, (), {}, 1, tmp_path / "log", tmp_path / "coverage")
    ran = support.Recorder(verdict)
    monkeypatch.setattr(executors.run_tests, "run", ran)
    for lease in leases:
        pod = support.pod_of(tmp_path / lease.head, support.FakeWorktrees(trees))
        suite = executors.Suite(pod.root / "manifest.json", pod.root / "flakes.jsonl")
        assert not suite.manifest.exists()
        finished = executors.tests(pod, suite, lease)
        assert finished.outcome == PASSED
        assert ran.calls[-1][0] == lease.unit.members
        assert cast("run_tests.Lab", ran.calls[-1][-1]).manifest == expected[lease.head]
        assert not suite.manifest.exists()


def restore_old_schema(run: Run, manifests: dict[str, dict[str, Any]], keep_jobs: bool = False) -> None:
    for state in run.grades():
        key = scheduler.GRADES + state.run
        raw = json.loads(run.lab.client.get(key))
        for slot in raw["units"].values():
            slot.pop("manifest")
            if not keep_jobs:
                run.lab.client.hdel(queue._job(slot["job"]), "manifest")
        run.lab.client.set(key, json.dumps(raw))
        path = f"{manifest_folder(scheduler._slug(state.repo))}{state.manifest}.json"
        manifest = copy.deepcopy(manifests[state.head])
        if keep_jobs:
            for record in manifest["tests"].values():
                record["outside"] = []
        run.lab.objects.write_once(path, json.dumps(dep_manifest.pack(manifest)).encode())
    old = run.engine
    run.engine = scheduler.Scheduler(run.lab.client, run.lab.objects, old.ports, old.settings)
