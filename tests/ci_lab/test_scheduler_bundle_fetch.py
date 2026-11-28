"""A lab worker fetches a test merge it lacks, issue 14197: the bundle of a graded commit excludes that commit's
first parent only, so a store without the pushed head applies it, and the expander fetches a commit no store holds
from the remote of each repository it serves before it bundles it, so a grade an earlier scheduler pod started
keeps its units.
"""

import base64
import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.ci_lab import budget, contract, executors, lease_api, merges, scheduler
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.units import Unit
from scripts.ci_lab.worker import Lease
from scripts.ci_lab.worktrees import Mirror
from scripts.dep_manifest import _git
from tests.ci_lab.test_executors import FakeWorktrees, pod_of
from tests.ci_lab.test_scheduler import BASE, HEAD_A, TREE, Consumer, FakeChecks, Run, _read, fake_redis, unit
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin
from tests.ci_lab.test_scheduler_merge_base import _expander, _moved
from tests.ci_lab.test_scheduler_merge_bundle import _pr
from tests.ci_lab.test_scheduler_merge_resolution import _synthetic_merge

MISSING = "f" * 40
LISTED_A = "https://lab.test/owner/a.git"
LISTED_B = "https://lab.test/owner/b.git"


def _prerequisites(bundle: bytes) -> set[str]:
    header = bundle.split(b"\n\n", 1)[0].decode()
    return {line[1:41] for line in header.splitlines() if line.startswith("-")}


def _clone_holding(tmp_path: Path, repo: Path, sha: str) -> Path:
    target = tmp_path / "target.git"
    _git(tmp_path, "init", "-q", "--bare", str(target))
    _git(repo, "branch", "-f", "held", sha)
    _git(target, "fetch", "--quiet", str(repo), "held:refs/heads/held")
    return target


def _kind(store: Path, sha: str) -> str:
    return subprocess.run(["git", "-C", str(store), "cat-file", "-t", sha], capture_output=True, text=True).stdout


def _applied(tmp_path: Path, target: Path, bundle: bytes, sha: str) -> str:
    bundle_file = tmp_path / "found.bundle"
    bundle_file.write_bytes(bundle)
    _git(target, "fetch", "--quiet", str(bundle_file), f"{sha}:refs/heads/merge")
    return _git(target, "rev-parse", "refs/heads/merge").strip()


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_a_store_that_holds_the_first_parent_and_not_the_head_fetches_the_merge_from_its_bundle(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    moved = _moved(repo, fork, {})
    merge = _synthetic_merge(repo, moved, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    expander.mirror(f"file://{repo}").fetch(merge)
    found = expander.bundle(merge)
    assert found is not None
    target = _clone_holding(tmp_path, repo, moved)
    assert _kind(target, head) == ""
    assert _applied(tmp_path, target, found, merge) == merge
    assert _kind(target, head) == "commit\n"


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_the_bundle_of_a_test_merge_is_prerequisite_on_its_first_parent_and_the_fork_point_never_on_its_head(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    moved = _moved(repo, fork, {})
    merge = _synthetic_merge(repo, moved, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    expander.mirror(f"file://{repo}").fetch(merge)
    found = expander.bundle(merge)
    assert found is not None
    assert _prerequisites(found) == {moved, fork}


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_a_worker_store_without_the_pushed_head_holds_the_test_merge_after_ensure_graded(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    moved = _moved(repo, fork, {})
    merge = _synthetic_merge(repo, moved, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    expander.mirror(f"file://{repo}").fetch(merge)
    node = tmp_path / "worker-node.git"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    _git(node, "fetch", "--quiet", str(repo), "moved:refs/heads/dev")
    (tmp_path / "worker").mkdir()
    mirror = Mirror(node, tmp_path / "worker" / "store", str(node))
    made = pod_of(tmp_path, FakeWorktrees({}, mirror=mirror))
    pod = executors.Pod(
        made.trees,
        made.root,
        made.ports,
        made.whitelist,
        made.timeout,
        made.clock,
        bundler=lambda job, token, sha: expander.bundle(sha),
    )
    lease = Lease("job", "tok", head, Unit("static", "k", ("lint",)), 1, 1500, moved, moved, merge)
    executors._ensure_graded(pod, lease)
    assert _kind(mirror.store, merge) == "commit\n"


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_bundle_fetches_a_commit_no_store_holds_from_the_served_remote_that_holds_it(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    merge = _synthetic_merge(repo, fork, head, monkeypatch)
    other = tmp_path / "other"
    _git(tmp_path, "init", "-q", "-b", "dev", str(other))
    expander = _expander(tmp_path, monkeypatch)
    expander.serve([f"file://{other}", f"file://{repo}"])
    found = expander.bundle(merge)
    assert found is not None
    target = _clone_holding(tmp_path, repo, fork)
    assert _applied(tmp_path, target, found, merge) == merge


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_bundle_answers_none_for_a_commit_no_served_remote_holds(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, _ = origin
    expander = _expander(tmp_path, monkeypatch)
    expander.serve([f"file://{repo}"])
    assert expander.bundle(MISSING) is None


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_bundle_bounds_each_fetch_of_a_missing_commit_and_answers_none_when_every_one_runs_past_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[str, str, float | None]] = []

    def slow(self: Mirror, sha: str, timeout: float | None = None) -> bool:
        asked.append((self.remote, sha, timeout))
        raise subprocess.TimeoutExpired(["git", "fetch"], merges.GIT_TIMEOUT)

    monkeypatch.setattr(Mirror, "fetch", slow)
    expander = _expander(tmp_path, monkeypatch)
    expander.serve([LISTED_A, LISTED_B])
    assert expander.bundle(MISSING) is None
    assert asked == [(LISTED_A, MISSING, merges.GIT_TIMEOUT), (LISTED_B, MISSING, merges.GIT_TIMEOUT)]


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_bundle_answers_the_bundle_of_a_commit_a_store_holds_after_its_fetch_failed(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    merge = _synthetic_merge(repo, fork, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    expander.serve([f"file://{repo}"])
    real_fetch = Mirror.fetch

    def lost(self: Mirror, sha: str, timeout: float | None = None) -> bool:
        real_fetch(self, sha, timeout)
        raise subprocess.CalledProcessError(128, ["git", "fetch"])

    monkeypatch.setattr(Mirror, "fetch", lost)
    found = expander.bundle(merge)
    assert found is not None
    target = _clone_holding(tmp_path, repo, fork)
    assert _applied(tmp_path, target, found, merge) == merge


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_bundle_answers_none_for_an_id_a_served_remote_gives_that_names_no_commit(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, head = origin
    tree = _git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    expander = _expander(tmp_path, monkeypatch)
    expander.serve([f"file://{repo}"])
    assert expander.bundle(tree) is None
    assert _kind(expander.mirror(f"file://{repo}").store, tree) == "tree\n"


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_two_requests_for_one_missing_commit_at_once_fetch_it_once(
    tmp_path: Path, origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    merge = _synthetic_merge(repo, fork, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    expander.serve([f"file://{repo}"])
    fetching, release = threading.Event(), threading.Event()
    fetches: list[str] = []
    found: list[bytes | None] = [None, None]
    errors: list[BaseException] = []
    real_fetch = Mirror.fetch

    def paced(self: Mirror, sha: str, timeout: float | None = None) -> bool:
        fetches.append(sha)
        fetching.set()
        release.wait(10.0)
        return real_fetch(self, sha, timeout)

    def ask(slot: int) -> None:
        try:
            found[slot] = expander.bundle(merge)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(Mirror, "fetch", paced)
    first, second = threading.Thread(target=ask, args=(0,)), threading.Thread(target=ask, args=(1,))
    first.start()
    assert fetching.wait(10.0)
    second.start()
    second.join(0.5)
    waiting = second.is_alive()
    release.set()
    first.join()
    second.join()
    assert waiting
    assert errors == []
    assert fetches == [merge]
    assert found[0] is not None and found[1] is not None


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
def test_the_bundle_route_answers_the_bundle_of_a_commit_the_scheduler_store_never_held(
    tmp_path: Path, origin: tuple[Path, str, str], lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fork, head = origin
    merge = _synthetic_merge(repo, fork, head, monkeypatch)
    expander = _expander(tmp_path, monkeypatch)
    expander.serve([f"file://{repo}"])
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=fork, graded_commit=merge)})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=(merge, fork)), bundle=expander.bundle)
    _pr(run, HEAD_A, 41, HEAD_A)
    run.tick(1000)
    spending = budget.Budget(run.lab.client, lambda: 1000, budget.Policy(100.0, {}))
    routes = lease_api.ControlRoutes(run.lab.client, "tok", lambda: 1000)
    servers = scheduler.listen(run.engine, run.leases(), routes, spending, lambda: 1000, (0, 0, 0))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{servers[0].server_address[1]}"
    try:
        body = json.dumps({"worker": "w", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
        leased = json.loads(_read(f"{api}/lease", body))["leases"]
        job, token = leased[0]["job"], leased[0]["token"]
        reply = json.loads(_read(f"{api}/bundle", json.dumps({"job": job, "token": token}).encode()))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    target = _clone_holding(tmp_path, repo, fork)
    assert _applied(tmp_path, target, base64.b64decode(reply["bundle"]), merge) == merge


@pytest.mark.feature("lab-worker-fetches-a-test-merge-it-lacks")
@pytest.mark.parametrize(
    ("listed", "expected"),
    [({}, [LISTED_A]), ({contract.SERVED_REPOSITORIES: f"{LISTED_B}, {LISTED_A}"}, [LISTED_B, LISTED_A])],
)
def test_build_names_every_served_repository_to_the_expander_for_the_fetch_of_a_missing_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listed: dict[str, str], expected: list[str]
) -> None:
    asked: list[str] = []

    def absent(self: Mirror, sha: str, timeout: float | None = None) -> bool:
        asked.append(self.remote)
        raise subprocess.CalledProcessError(128, ["git", "fetch"])

    monkeypatch.setattr(Mirror, "fetch", absent)
    monkeypatch.setattr(scheduler.MirrorExpander, "trusted", lambda self, repo: frozenset({repo}))
    monkeypatch.setattr(scheduler.MirrorExpander, "retries", lambda self, repo: Policy())
    monkeypatch.setattr(scheduler, "Checks", FakeChecks)
    environ = {
        contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: LISTED_A,
        scheduler.IMAGE: "image",
        **listed,
    }
    client = fake_redis()
    spending = budget.Budget(client, lambda: 0, budget.Policy(100.0, {}))
    built = scheduler.build(environ, client, LocalObjects(tmp_path / "objects"), Consumer([]), spending)
    assert built.ports.bundle is not None
    assert built.ports.bundle(MISSING) is None
    assert asked == expected
