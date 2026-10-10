import hashlib
import os
import re
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import count
from pathlib import Path

import pytest

from scripts.swarm import naming
from scripts.swarm_v2 import filesystem, workspaces

pytestmark = pytest.mark.unit

AGENT = "engineer@abc123-0007"


def git(*args, cwd=None) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def commit(work: Path, name: str) -> str:
    (work / name).write_text(name)
    git("add", name, cwd=work)
    git("commit", "-q", "-m", name, cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


def upstream(root: Path, name: str) -> tuple[Path, Path]:
    work, bare = root / f"{name}-work", root / f"{name}.git"
    git("init", "-q", "-b", "dev", str(work))
    commit(work, "base")
    git("init", "-q", "--bare", str(bare))
    git("push", "-q", str(bare), "dev", cwd=work)
    return work, bare


def refs(mirror: Path) -> str:
    return git("for-each-ref", "--format=%(objectname) %(refname)", cwd=mirror)


def fake_clock(step: float = 0.25):
    ticks = count()
    return lambda: 1 + next(ticks) * step


class World:
    def __init__(self, root: Path):
        self.root = root
        self.work, self.origin = upstream(root, "origin")
        self.other_work, self.other = upstream(root, "other")
        self.url = f"file://{self.origin}"
        self.project = workspaces.identity(self.url)
        base = root / "attempts"
        base.mkdir()
        self.layout = filesystem.load()
        self.base = base
        self.execution = filesystem.allocate(base, "a1", self.layout)

    def request(self, task: str = "t1", generation: int = 1, **extra) -> workspaces.Request:
        return workspaces.Request(origin=self.url, task=task, generation=generation, agent=AGENT, **extra)

    def head(self) -> str:
        return git("rev-parse", "dev", cwd=self.work)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Org/Repo.git",
        "https://user:secret@github.com/Org/Repo/",
        "ssh://git@GitHub.com/Org/Repo.git",
        "git@github.com:Org/Repo.git",
        " https://github.com/Org/Repo ",
    ],
)
def test_origin_identity_is_normalized_and_credential_free(url):
    assert workspaces.identity(url) == "github.com/Org/Repo"


def test_origin_identity_keeps_a_port_and_reads_local_paths():
    assert workspaces.identity("ssh://git@host.example:2222/a/b.git") == "host.example:2222/a/b"
    assert workspaces.identity("file:///srv/git/repo.git") == "/srv/git/repo"


@pytest.mark.parametrize("url", ["", "ftp://host/a/b", "https://github.com/", "git@github.com:", "not a url"])
def test_unsupported_origin_is_refused(url):
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.identity(url)
    assert str(refused.value) == "unsupported origin"


def test_different_repositories_have_different_identities():
    assert workspaces.identity("https://github.com/org/a") != workspaces.identity("https://github.com/org/b")
    assert workspaces.identity("https://github.com/org/a") != workspaces.identity("https://gitlab.com/org/a")


def test_prepare_starts_the_agent_in_an_isolated_worktree_at_the_fresh_base(world):
    prepared = workspaces.prepare(world.execution, world.request(), clock=fake_clock())
    branch = naming.worktree({"AGENTIHOOKS_AGENT_NAME": AGENT})
    assert prepared.branch == branch == "engineer-abc123-0007"
    assert prepared.path == world.execution.path("worktree") / branch
    assert prepared.mirror.parent == world.execution.path("checkout")
    assert prepared.project == world.project
    assert prepared.base == "dev"
    assert prepared.base_commit == world.head()
    assert (prepared.task, prepared.generation) == ("t1", 1)
    assert prepared.workspace_prepare_seconds == 0.25
    assert git("rev-parse", "HEAD", cwd=prepared.path) == world.head()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=prepared.path) == branch
    assert git("status", "--porcelain", cwd=prepared.path) == ""
    assert workspaces.identity(git("config", "remote.origin.url", cwd=prepared.path)) == world.project


def test_prepare_records_origin_and_base_before_the_first_edit(world):
    prepared = workspaces.prepare(world.execution, world.request(), clock=fake_clock())
    assert workspaces.recorded(world.execution, "t1") == {
        "path": str(prepared.path),
        "branch": prepared.branch,
        "mirror": str(prepared.mirror),
        "project": world.project,
        "base": "dev",
        "base_commit": world.head(),
        "task": "t1",
        "generation": 1,
        "workspace_prepare_seconds": 0.25,
    }
    assert workspaces.recorded(world.execution, "t2") is None


def test_prepare_fetches_new_base_commits_into_the_cached_mirror(world):
    first = workspaces.prepare(world.execution, world.request("t1"))
    newer = commit(world.work, "newer")
    git("push", "-q", str(world.origin), "dev", cwd=world.work)
    second = workspaces.prepare(world.execution, world.request("t2"))
    assert second.mirror == first.mirror
    assert first.base_commit != newer
    assert second.base_commit == newer
    assert second.branch == "engineer-abc123-0007-2"
    assert git("rev-parse", "HEAD", cwd=second.path) == newer


def test_prepare_takes_the_requested_base_branch(world):
    git("checkout", "-q", "-b", "feature", cwd=world.work)
    feature = commit(world.work, "feature")
    git("push", "-q", str(world.origin), "feature", cwd=world.work)
    prepared = workspaces.prepare(world.execution, world.request(base="feature"))
    assert (prepared.base, prepared.base_commit) == ("feature", feature)


def test_a_missing_base_branch_is_refused(world):
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request(base="nowhere"))
    assert str(refused.value) == f"base nowhere is missing from {world.project}"
    assert not list(world.execution.path("worktree").iterdir())


def test_replaying_the_same_task_generation_returns_the_same_workspace(world):
    first = workspaces.prepare(world.execution, world.request(), clock=fake_clock())
    replayed = workspaces.prepare(world.execution, world.request(), clock=fake_clock(9))
    assert replayed == first
    assert [p.name for p in world.execution.path("worktree").iterdir()] == [first.branch]


def test_a_newer_generation_gets_a_new_worktree_and_record(world):
    first = workspaces.prepare(world.execution, world.request(generation=1))
    newer = workspaces.prepare(world.execution, world.request(generation=2))
    assert newer.path != first.path
    assert workspaces.recorded(world.execution, "t1")["generation"] == 2


def test_an_older_generation_cannot_overwrite_a_newer_record(world):
    workspaces.prepare(world.execution, world.request(generation=3))
    before = workspaces.recorded(world.execution, "t1")
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request(generation=2))
    assert str(refused.value) == "task t1 generation 2 is older than its recorded generation 3"
    assert workspaces.recorded(world.execution, "t1") == before


def test_a_recorded_generation_for_another_project_is_refused(world):
    workspaces.prepare(world.execution, world.request())
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, replace(world.request(), origin=f"file://{world.other}"))
    assert str(refused.value) == "task t1 generation 1 is recorded for another project"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"task": "../t"}, "invalid task id: ../t"),
        ({"task": ""}, "invalid task id: "),
        ({"generation": 0}, "invalid generation: 0"),
        ({"base": "-x"}, "invalid base branch: -x"),
        ({"base": ""}, "invalid base branch: "),
    ],
)
def test_invalid_requests_are_refused_before_any_git_io(world, change, message):
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, replace(world.request(), **change))
    assert str(refused.value) == message
    assert [p.name for p in world.execution.path("checkout").iterdir()] == []


def test_a_wrong_origin_mirror_is_not_reused_by_its_folder_name(world):
    mirror = workspaces.mirror_path(world.execution, world.project)
    git("clone", "-q", "--bare", f"file://{world.other}", str(mirror))
    before = (refs(mirror), git("config", "remote.origin.url", cwd=mirror))
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request())
    assert str(refused.value) == (
        f"cached mirror {mirror.name} has origin {workspaces.identity(f'file://{world.other}')}, "
        f"not {world.project}; it is not reused"
    )
    assert (refs(mirror), git("config", "remote.origin.url", cwd=mirror)) == before
    assert not list(world.execution.path("worktree").iterdir())
    assert workspaces.recorded(world.execution, "t1") is None


def test_a_folder_without_an_origin_is_not_reused(world):
    mirror = workspaces.mirror_path(world.execution, world.project)
    git("init", "-q", "--bare", str(mirror))
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request())
    assert (
        str(refused.value) == f"cached mirror {mirror.name} has origin unknown, not {world.project}; it is not reused"
    )


def test_the_mirror_path_is_derived_from_the_full_project_identity(world):
    first = workspaces.mirror_path(world.execution, "github.com/org/repo")
    assert first.parent == world.execution.path("checkout")
    assert first.name.startswith("repo-") and first.name.endswith(".git")
    assert first != workspaces.mirror_path(world.execution, "gitlab.com/org/repo")
    assert first == workspaces.mirror_path(world.execution, "github.com/org/repo")


def test_a_fetch_failure_leaves_the_cache_untouched_and_starts_no_work(world):
    first = workspaces.prepare(world.execution, world.request("t1"))
    commit(world.work, "unseen")
    git("push", "-q", str(world.origin), "dev", cwd=world.work)
    before = refs(first.mirror)
    world.origin.rename(world.root / "gone.git")
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request("t2"))
    assert (
        str(refused.value) == f"fetch of {world.project} failed; the cached base is unverified, so work does not start"
    )
    assert refs(first.mirror) == before
    assert [p.name for p in world.execution.path("worktree").iterdir()] == [first.branch]
    assert workspaces.recorded(world.execution, "t2") is None
    (world.root / "gone.git").rename(world.origin)
    recovered = workspaces.prepare(world.execution, world.request("t2"))
    assert recovered.base_commit == world.head()


def test_a_base_without_the_required_commit_is_refused_as_stale(world):
    git("checkout", "-q", "-b", "side", cwd=world.work)
    side = commit(world.work, "side")
    git("push", "-q", str(world.origin), "side", cwd=world.work)
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request(minimum=side))
    assert str(refused.value) == f"base dev at {world.head()} does not contain {side}; it is stale"
    assert not list(world.execution.path("worktree").iterdir())
    assert workspaces.recorded(world.execution, "t1") is None


def test_a_base_containing_the_required_commit_is_accepted(world):
    required = world.head()
    newer = commit(world.work, "newer")
    git("push", "-q", str(world.origin), "dev", cwd=world.work)
    assert workspaces.prepare(world.execution, world.request(minimum=required)).base_commit == newer


def test_a_failed_clone_leaves_no_partial_mirror(world):
    missing = f"file://{world.root / 'missing.git'}"
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, replace(world.request(), origin=missing))
    assert str(refused.value) == f"clone of {workspaces.identity(missing)} failed"
    assert [p.name for p in world.execution.path("checkout").iterdir()] == [workspaces.LOCK]


def test_concurrent_preparations_clone_once_and_get_distinct_worktrees(world):
    tasks = [f"t{n}" for n in range(4)]
    with ThreadPoolExecutor(len(tasks)) as pool:
        prepared = list(pool.map(lambda task: workspaces.prepare(world.execution, world.request(task)), tasks))
    assert len({p.path for p in prepared}) == len(tasks)
    assert {p.mirror for p in prepared} == {workspaces.mirror_path(world.execution, world.project)}
    assert sorted(p.name for p in world.execution.path("checkout").glob("*.git")) == [prepared[0].mirror.name]
    assert {git("rev-parse", "HEAD", cwd=p.path) for p in prepared} == {world.head()}


def test_concurrent_replays_of_one_task_generation_create_one_worktree(world):
    with ThreadPoolExecutor(3) as pool:
        prepared = list(pool.map(lambda _: workspaces.prepare(world.execution, world.request()), range(3)))
    assert len(set(prepared)) == 1
    assert len(list(world.execution.path("worktree").iterdir())) == 1


def test_two_executions_keep_private_mirrors_and_worktrees(world):
    other = filesystem.allocate(world.base, "a2", world.layout)
    first = workspaces.prepare(world.execution, world.request())
    second = workspaces.prepare(other, world.request())
    assert first.path.relative_to(world.execution.root)
    assert second.path.relative_to(other.root)
    assert first.mirror.relative_to(world.execution.root)
    assert second.mirror.relative_to(other.root)


def test_disabled_cache_reuse_clones_fresh_beside_the_cached_mirror(world):
    cached = workspaces.prepare(world.execution, world.request("t1"))
    fresh = workspaces.prepare(world.execution, world.request("t2"), reuse=False)
    assert fresh.mirror != cached.mirror
    assert fresh.mirror.parent == cached.mirror.parent
    assert fresh.base_commit == cached.base_commit
    assert workspaces.identity(git("config", "remote.origin.url", cwd=fresh.mirror)) == world.project


def test_disabled_cache_reuse_ignores_a_wrong_origin_folder(world):
    mirror = workspaces.mirror_path(world.execution, world.project)
    git("clone", "-q", "--bare", f"file://{world.other}", str(mirror))
    fresh = workspaces.prepare(world.execution, world.request(), reuse=False)
    assert fresh.mirror != mirror
    assert fresh.base_commit == world.head()


def test_scp_origin_without_a_user_is_read():
    assert workspaces.identity("github.com:Org/Repo.git") == "github.com/Org/Repo"


@pytest.mark.parametrize(("project", "slug"), [("host/@@", "repo"), ("host/-My.Repo-", "my.repo")])
def test_the_mirror_folder_name_is_a_clean_slug_and_a_digest(world, project, slug):
    digest = hashlib.sha256(project.encode()).hexdigest()[:16]
    assert workspaces.mirror_path(world.execution, project).name == f"{slug}-{digest}.git"


def test_a_fresh_clone_name_adds_an_attempt_suffix(world):
    name = workspaces.mirror_path(world.execution, world.project, reuse=False).name
    digest = hashlib.sha256(world.project.encode()).hexdigest()[:16]
    assert re.fullmatch(rf"origin-{digest}-[0-9a-f]{{8}}\.git", name)


def test_a_mirror_with_an_unreadable_origin_is_not_reused(world):
    mirror = workspaces.mirror_path(world.execution, world.project)
    git("init", "-q", "--bare", str(mirror))
    git("config", "remote.origin.url", "ftp://host/a", cwd=mirror)
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request())
    assert (
        str(refused.value) == f"cached mirror {mirror.name} has origin unknown, not {world.project}; it is not reused"
    )


def test_a_branch_deleted_upstream_is_pruned_from_the_cache(world):
    git("push", "-q", str(world.origin), "dev:gone", cwd=world.work)
    workspaces.prepare(world.execution, world.request("t1", base="gone"))
    git("push", "-q", str(world.origin), ":gone", cwd=world.work)
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request("t2", base="gone"))
    assert str(refused.value) == f"base gone is missing from {world.project}"


def test_a_branch_name_held_by_the_mirror_is_not_reused(world):
    git("push", "-q", str(world.origin), "dev:engineer-abc123-0007", cwd=world.work)
    assert workspaces.prepare(world.execution, world.request()).branch == "engineer-abc123-0007-2"


def test_a_worktree_that_git_refuses_is_reported_and_not_recorded(world, monkeypatch):
    monkeypatch.setattr(workspaces, "_branch", lambda *_: "dev")
    with pytest.raises(workspaces.WorkspaceError) as refused:
        workspaces.prepare(world.execution, world.request())
    assert str(refused.value) == "worktree dev could not be created"
    assert workspaces.recorded(world.execution, "t1") is None


def test_the_record_folder_is_private(world):
    workspaces.prepare(world.execution, world.request())
    folder = world.execution.path("spool") / "workspaces"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_git_runs_without_prompting_and_with_a_timeout(world, monkeypatch):
    seen = []
    real = subprocess.run

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(workspaces.subprocess, "run", spy)
    workspaces.prepare(world.execution, world.request())
    assert seen
    assert {(kw["env"]["GIT_TERMINAL_PROMPT"], kw["timeout"], kw["text"]) for kw in seen} == {("0", 600, True)}
    assert all(kw["env"]["PATH"] == os.environ["PATH"] for kw in seen)


def test_package_cases_pass_on_the_isolated_fixture():
    from tests.sv2_fsy02_cases import case_a, case_b, case_c

    assert [case()["passed"] for case in (case_a, case_b, case_c)] == [True, True, True]
