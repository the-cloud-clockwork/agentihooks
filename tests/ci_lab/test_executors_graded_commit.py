"""The lease and the executor context of a static unit carry the test merge GitHub built for its pull request, issue
13890: the tree and the two commit fields a static unit's lease and context read.
"""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci_lab import executors, lease_api, leases, queue, static_gates
from scripts.ci_lab.leases import Grant
from scripts.ci_lab.units import Unit
from scripts.ci_lab.worker import Lease, _lease
from tests.ci_lab.test_executors import FakeWorktrees, folder, pod_of
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
    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.fetched: list[str] = []

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
