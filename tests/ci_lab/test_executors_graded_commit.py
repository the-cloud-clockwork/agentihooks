"""The lease and the executor context of a static unit carry the test merge GitHub built for its pull request, issue
13890: the tree and the two commit fields a static unit's lease and context read.
"""

import hashlib
import json
import subprocess
import types
from pathlib import Path

import pytest

from scripts.ci_lab import executors, lease_api, leases, queue, static_gates
from scripts.ci_lab.leases import Grant
from scripts.ci_lab.outcomes import PASSED
from scripts.ci_lab.units import Unit
from scripts.ci_lab.worker import Client, Lease, _lease
from scripts.dep_manifest import _git
from tests.ci_lab.test_executors import FakeWorktrees, environ_of, folder, pod_of
from tests.ci_lab.test_lease_api import Signer, post
from tests.ci_lab.test_lease_api import clock as _clock
from tests.ci_lab.test_lease_api import jobs as _jobs
from tests.ci_lab.test_lease_api import lab as _lab
from tests.ci_lab.test_lease_api import signer as _signer

clock = _clock
jobs = _jobs
lab = _lab
signer = _signer

MERGE = "m" * 40
WORKFLOW = """
on: pull_request
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Lint check
        run: python -m ruff check scripts/
"""
REGISTRY = {"lint/lint-check": {"paths": ["scripts"], "tools": ["ruff"]}}


def _gate() -> static_gates.StaticGate:
    [found] = static_gates.discover(WORKFLOW, REGISTRY)
    return found


class FakeMirror:
    def __init__(self, fails: bool = False, store: Path | None = None) -> None:
        self.fails = fails
        self.fetched: list[str] = []
        self.store = store

    def fetch(self, sha: str) -> bool:
        self.fetched.append(sha)
        if self.fails:
            raise subprocess.CalledProcessError(1, ["git", "fetch"])
        return False


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_context_of_a_lease_with_a_graded_commit_reads_github_sha_as_that_commit() -> None:
    lease = Lease("job", "tok", "h1", Unit("static", "k", ("lint",)), 1, 1500, "b1", "g1", MERGE)
    context = executors._context(lease, _gate(), {})
    assert context["github.sha"] == MERGE
    assert context["github.event.pull_request.head.sha"] == "h1"
    assert context["github.event.pull_request.base.sha"] == "b1"


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_context_of_a_lease_without_a_graded_commit_reads_github_sha_as_its_head() -> None:
    lease = Lease("job", "tok", "h1", Unit("static", "k", ("lint",)), 1, 1500, "b1", "g1")
    context = executors._context(lease, _gate(), {})
    assert context["github.sha"] == "h1"


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_static_cuts_its_shared_tree_from_the_graded_commit_when_the_lease_names_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.ci_lab import run_static

    merged = folder(tmp_path, "merged", {".github/test-requirements.lock": "lock-m"})
    base = folder(tmp_path, "base", {".github/test-requirements.lock": "lock-b"})
    grader = folder(
        tmp_path,
        "grader",
        {
            ".github/workflows/test.yml": WORKFLOW,
            ".github/input-keys.json": json.dumps(REGISTRY),
            ".github/test-requirements.lock": "lock-g",
        },
    )
    trees = FakeWorktrees({MERGE: merged, "b1": base, "g1": grader}, mirror=FakeMirror())
    pod = pod_of(tmp_path, trees)
    name = _gate().name
    summary = folder(tmp_path, "run", {"summary.md": "ok\n"}) / "summary.md"

    def ran(*_args: object) -> run_static.Result:
        return run_static.Result(name, 0, tmp_path / "log.txt", summary)

    monkeypatch.setattr(executors.run_static, "run", ran)
    monkeypatch.setattr(executors.venvs, "venv_for", lambda *_a, **_kw: (tmp_path / "venv", False))
    lease = Lease("job", "tok", "h1", Unit("static", "k", (name,)), 1, 1500, "b1", "g1", MERGE)

    executors.static(pod, lease, pod.runs)

    assert ("lease", MERGE) in trees.calls
    assert ("lease", "h1") not in trees.calls
    assert trees.mirror.fetched == [MERGE]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_tests_runs_its_batch_on_the_graded_commit_when_the_lease_names_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.ci_lab import run_tests

    merged = folder(tmp_path, "merged", {".github/test-requirements.lock": "lock-m"})
    manifest = {"tests": {"tests/a.py::t": {"files": ["scripts/a.py"]}}}
    suite = executors.Suite(
        folder(tmp_path, "in", {"manifest.json": json.dumps(manifest)}) / "manifest.json", tmp_path / "flakes.jsonl"
    )
    verdict = run_tests.Verdict(PASSED, False, (), {}, 1, tmp_path / "log.txt", tmp_path / "cov")

    def ran(*_args: object) -> run_tests.Verdict:
        return verdict

    monkeypatch.setattr(executors.run_tests, "run", ran)
    monkeypatch.setattr(executors.venvs, "venv_for", lambda *_a, **_kw: (tmp_path / "venv", False))
    trees = FakeWorktrees({MERGE: merged}, mirror=FakeMirror())
    pod = pod_of(tmp_path, trees)
    lease = Lease(
        "job",
        "tok",
        "h1",
        Unit(
            "test",
            "k",
            ("tests/a.py::t",),
            json.dumps(manifest),
            hashlib.sha256(json.dumps(manifest).encode()).hexdigest(),
        ),
        1,
        1500,
        "b1",
        "g1",
        MERGE,
    )

    executors.tests(pod, suite, lease, pod.runs)

    assert ("lease", MERGE) in trees.calls
    assert ("lease", "h1") not in trees.calls
    assert trees.mirror.fetched == [MERGE]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_ensure_graded_asks_the_bundler_outside_the_tree_lock(tmp_path: Path) -> None:
    trees = FakeWorktrees({}, mirror=FakeMirror(fails=True))
    base_pod = pod_of(tmp_path, trees)
    acquired: list[bool] = []

    def bundler(job: str, token: str, sha: str) -> bytes | None:
        free = base_pod.trees.lock.acquire(blocking=False)
        acquired.append(free)
        if free:
            base_pod.trees.lock.release()
        return None

    pod = executors.Pod(
        base_pod.trees,
        base_pod.root,
        base_pod.ports,
        base_pod.whitelist,
        base_pod.timeout,
        base_pod.clock,
        bundler=bundler,
    )
    lease = Lease("job", "tok", "h1", Unit("static", "k", ("lint",)), 1, 1500, "b1", "g1", MERGE)
    with pytest.raises(subprocess.CalledProcessError):
        executors._ensure_graded(pod, lease)
    assert acquired == [True]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_static_cuts_its_owned_tree_from_the_graded_commit_when_its_gate_rewrites_the_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.ci_lab import run_static

    workflow = """
on: pull_request
jobs:
  semgrep:
    runs-on: ubuntu-latest
    steps:
      - name: Scan
        run: semgrep --baseline-commit HEAD^
"""
    registry = {"semgrep/scan": {"paths": ["scripts"], "tools": ["semgrep"]}}
    merged = folder(tmp_path, "merged", {".github/test-requirements.lock": "lock-m"})
    base = folder(tmp_path, "base", {".github/test-requirements.lock": "lock-b"})
    grader = folder(
        tmp_path,
        "grader",
        {
            ".github/workflows/test.yml": workflow,
            ".github/input-keys.json": json.dumps(registry),
            ".github/test-requirements.lock": "lock-g",
        },
    )
    head_tree = folder(tmp_path, "head", {})
    trees = FakeWorktrees({MERGE: merged, "b1": base, "g1": grader, "h1": head_tree}, mirror=FakeMirror())
    pod = pod_of(tmp_path, trees)
    [found] = static_gates.discover(workflow, registry)
    assert found.rewrites_tree
    summary = folder(tmp_path, "run", {"summary.md": "ok\n"}) / "summary.md"

    def ran(*_args: object) -> run_static.Result:
        return run_static.Result(found.name, 0, tmp_path / "log.txt", summary)

    monkeypatch.setattr(executors.run_static, "run", ran)
    monkeypatch.setattr(executors.venvs, "venv_for", lambda *_a, **_kw: (tmp_path / "venv", False))
    lease = Lease("job", "tok", "h1", Unit("static", "k", (found.name,)), 1, 1500, "b1", "g1", MERGE)

    executors.static(pod, lease, pod.runs)

    assert ("own", MERGE, "b1") in trees.calls
    assert ("own", "h1", "b1") not in trees.calls


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_ensure_graded_does_nothing_more_once_the_mirror_already_holds_the_commit(tmp_path: Path) -> None:
    trees = FakeWorktrees({}, mirror=FakeMirror())
    base_pod = pod_of(tmp_path, trees)
    calls: list[tuple[str, str, str]] = []

    def bundler(job: str, token: str, sha: str) -> bytes | None:
        calls.append((job, token, sha))
        return None

    pod = executors.Pod(
        base_pod.trees,
        base_pod.root,
        base_pod.ports,
        base_pod.whitelist,
        base_pod.timeout,
        base_pod.clock,
        bundler=bundler,
    )
    lease = Lease("job", "tok", "h1", Unit("static", "k", ("lint",)), 1, 1500, "b1", "g1", MERGE)
    executors._ensure_graded(pod, lease)
    assert trees.mirror.fetched == [MERGE]
    assert calls == []


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_ensure_graded_applies_the_bundle_the_bundler_serves(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _git(tmp_path, "init", "-q", str(source))
    _git(source, "config", "user.email", "t@t")
    _git(source, "config", "user.name", "t")
    (source / "a.txt").write_text("a\n")
    _git(source, "add", "a.txt")
    _git(source, "commit", "-qm", "root")
    root = _git(source, "rev-parse", "HEAD").strip()
    bundle_path = tmp_path / "root.bundle"
    _git(source, "bundle", "create", str(bundle_path), "HEAD")
    bundle_bytes = bundle_path.read_bytes()
    store = tmp_path / "store.git"
    _git(tmp_path, "init", "-q", "--bare", str(store))
    trees = FakeWorktrees({}, mirror=FakeMirror(fails=True, store=store))
    base_pod = pod_of(tmp_path, trees)
    calls: list[tuple[str, str, str]] = []

    def bundler(job: str, token: str, sha: str) -> bytes | None:
        calls.append((job, token, sha))
        return bundle_bytes

    pod = executors.Pod(
        base_pod.trees,
        base_pod.root,
        base_pod.ports,
        base_pod.whitelist,
        base_pod.timeout,
        base_pod.clock,
        bundler=bundler,
    )
    lease = Lease("job", "tok", "h1", Unit("static", "k", ("lint",)), 1, 1500, "b1", "g1", root)
    executors._ensure_graded(pod, lease)
    assert calls == [("job", "tok", root)]
    found = subprocess.run(["git", "-C", str(store), "cat-file", "-t", root], capture_output=True, text=True)
    assert found.stdout.strip() == "commit"


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_ensure_graded_asks_the_bundler_when_the_node_mirror_lacks_the_commit(tmp_path: Path) -> None:
    trees = FakeWorktrees({}, mirror=FakeMirror(fails=True))
    base_pod = pod_of(tmp_path, trees)
    calls: list[tuple[str, str, str]] = []

    def bundler(job: str, token: str, sha: str) -> bytes | None:
        calls.append((job, token, sha))
        return None

    pod = executors.Pod(
        base_pod.trees,
        base_pod.root,
        base_pod.ports,
        base_pod.whitelist,
        base_pod.timeout,
        base_pod.clock,
        bundler=bundler,
    )
    lease = Lease("job", "tok", "h1", Unit("static", "k", ("lint",)), 1, 1500, "b1", "g1", MERGE)
    with pytest.raises(subprocess.CalledProcessError):
        executors._ensure_graded(pod, lease)
    assert calls == [("job", "tok", MERGE)]


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_settings_wires_the_pods_bundler_to_the_scheduler_url(tmp_path: Path) -> None:
    pod, _, _ = executors.settings(environ_of(tmp_path))
    assert isinstance(pod.bundler, types.MethodType)
    assert isinstance(pod.bundler.__self__, Client)
    assert pod.bundler.__self__.url == "http://scheduler:8800"


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_worker_lease_decodes_its_graded_commit_from_the_grant() -> None:
    grant = {
        "job": "j",
        "token": "t",
        "head": "h1",
        "kind": "static",
        "key": "k",
        "members": ["lint"],
        "slots": 1,
        "expires_ms": 1500,
        "base": "b1",
        "grader": "g1",
        "graded_commit": MERGE,
    }
    assert _lease(grant).graded_commit == MERGE


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_worker_lease_without_a_graded_commit_defaults_to_empty() -> None:
    grant = {
        "job": "j",
        "token": "t",
        "head": "h1",
        "kind": "static",
        "key": "k",
        "members": ["lint"],
        "slots": 1,
        "expires_ms": 1500,
        "base": "b1",
        "grader": "g1",
    }
    assert _lease(grant).graded_commit == ""


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grant_with_a_graded_commit_carries_it_in_the_lease_api_reply() -> None:
    unit = Unit("static", "k", ("lint",))
    grant = Grant("j", "t", "h1", unit, 1, 1500, "b1", "g1", MERGE)
    assert lease_api._grant(grant)["graded_commit"] == MERGE


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_grant_without_a_graded_commit_omits_it_from_the_lease_api_reply() -> None:
    unit = Unit("static", "k", ("lint",))
    grant = Grant("j", "t", "h1", unit, 1, 1500, "b1", "g1")
    assert "graded_commit" not in lease_api._grant(grant)


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_granted_lease_carries_its_graded_commit(jobs: queue.Queue, lab: leases.Leases, signer: Signer) -> None:
    jobs.enqueue([Unit(kind="static", key="a", members=("lint",))], "h1", {}, "b1", "g1", graded_commit=MERGE)
    [grant] = lab.lease("w1", 1, frozenset(), 0)
    assert grant.graded_commit == MERGE


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_bundle_route_answers_a_base64_bundle_of_the_jobs_graded_commit(
    jobs: queue.Queue, lab: leases.Leases, signer: Signer
) -> None:
    import base64

    jobs.enqueue([Unit(kind="static", key="a", members=("lint",))], "h1", {}, "b1", "g1", graded_commit=MERGE)
    granted = post(lab, signer, "/lease", {"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0})
    [grant] = granted[1]["leases"]

    def bundler(sha: str) -> bytes | None:
        assert sha == MERGE
        return b"bundle-bytes"

    status, body = lease_api.respond(
        lab, signer, "POST", "/bundle", json.dumps({"job": grant["job"], "token": grant["token"]}).encode(), bundler
    )
    assert status == 200
    assert base64.b64decode(body["bundle"]) == b"bundle-bytes"


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_bundle_route_refuses_a_token_that_does_not_own_the_lease(lab: leases.Leases, signer: Signer) -> None:
    status, body = lease_api.respond(
        lab, signer, "POST", "/bundle", json.dumps({"job": "h1:static:a", "token": "wrong"}).encode()
    )
    assert (status, body) == (403, {"error": "lease token refused"})


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_bundle_route_answers_404_when_no_store_holds_the_commit(
    jobs: queue.Queue, lab: leases.Leases, signer: Signer
) -> None:
    jobs.enqueue([Unit(kind="static", key="a", members=("lint",))], "h1", {}, "b1", "g1", graded_commit=MERGE)
    granted = post(lab, signer, "/lease", {"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0})
    [grant] = granted[1]["leases"]
    status, body = lease_api.respond(
        lab,
        signer,
        "POST",
        "/bundle",
        json.dumps({"job": grant["job"], "token": grant["token"]}).encode(),
        lambda sha: None,
    )
    assert status == 404
    assert body == {"error": "no served repository's store holds the graded commit"}


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_bundle_route_refuses_a_job_with_no_graded_commit(
    jobs: queue.Queue, lab: leases.Leases, signer: Signer
) -> None:
    jobs.enqueue([Unit(kind="static", key="a", members=("lint",))], "h1", {}, "b1", "g1")
    granted = post(lab, signer, "/lease", {"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0})
    [grant] = granted[1]["leases"]
    status, body = lease_api.respond(
        lab,
        signer,
        "POST",
        "/bundle",
        json.dumps({"job": grant["job"], "token": grant["token"]}).encode(),
        lambda sha: b"unused",
    )
    assert (status, body) == (400, {"error": "bad request"})
