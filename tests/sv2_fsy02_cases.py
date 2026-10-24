import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from scripts.swarm_v2 import workspaces
from tests.test_swarm_v2_workspaces import World, commit, git, refs

FIXTURE_PASSWORD = "-".join(("fixture", "only"))
CREDENTIAL_URL = f"https://fixture:{FIXTURE_PASSWORD}@example.invalid/other/repo.git"


def _refusal(execution, request, reuse=True) -> str:
    try:
        workspaces.prepare(execution, request, reuse=reuse)
    except workspaces.WorkspaceError as error:
        return str(error)
    return ""


def _prepared(world: World, workspace: workspaces.Workspace) -> dict:
    return {
        "inside_execution_root": workspace.path.is_relative_to(world.execution.root),
        "mirror_inside_execution_root": workspace.mirror.is_relative_to(world.execution.root),
        "at_fresh_base": git("rev-parse", "HEAD", cwd=workspace.path) == world.head() == workspace.base_commit,
        "branch_from_naming_helper": workspace.branch == "engineer-abc123-0007",
        "project_matches_origin": workspace.project == world.project,
        "record_matches": workspaces.recorded(world.execution, workspace.task)["base_commit"] == world.head(),
        "task_generation_matches": (workspace.task, workspace.generation) == ("t1", 1),
        "workspace_prepare_seconds_measured": workspace.workspace_prepare_seconds >= 0,
    }


def _concurrent() -> dict:
    with tempfile.TemporaryDirectory() as root:
        world = World(Path(root))
        tasks = [f"c{n}" for n in range(4)]
        with ThreadPoolExecutor(len(tasks)) as pool:
            together = list(pool.map(lambda task: workspaces.prepare(world.execution, world.request(task)), tasks))
        checks = {
            "distinct_worktrees": len({w.path for w in together}) == len(tasks),
            "one_mirror": len(list(world.execution.path("checkout").glob("*.git"))) == 1,
            "all_at_fresh_base": {w.base_commit for w in together} == {world.head()},
        }
    seconds = sorted(round(w.workspace_prepare_seconds, 3) for w in together)
    return {**checks, "passed": all(checks.values()), "workspace_prepare_seconds": seconds}


def case_a():
    runs = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as root:
            world = World(Path(root))
            workspace = workspaces.prepare(world.execution, world.request())
            checks = _prepared(world, workspace)
            checks["replay_returns_same_workspace"] = workspaces.prepare(world.execution, world.request()) == workspace
            checks["one_worktree"] = len(list(world.execution.path("worktree").iterdir())) == 1
            seconds = round(workspace.workspace_prepare_seconds, 3)
            runs.append({**checks, "passed": all(checks.values()), "workspace_prepare_seconds": seconds})
    concurrent = _concurrent()
    return {
        "then": "an agent begins in an isolated worktree tied to the correct project and task generation",
        "passed": all(run["passed"] for run in runs) and concurrent["passed"],
        "independent_fixtures": runs,
        "concurrent_preparation": concurrent,
    }


def case_b():
    with tempfile.TemporaryDirectory() as root:
        world = World(Path(root))
        mirror = workspaces.mirror_path(world.execution, world.project)
        git("clone", "-q", "--bare", f"file://{world.other}", str(mirror))
        git("config", "remote.origin.url", CREDENTIAL_URL, cwd=mirror)
        before = (refs(mirror), git("config", "remote.origin.url", cwd=mirror))
        message = _refusal(world.execution, world.request())
        expected = (
            f"cached mirror {mirror.name} has origin example.invalid/other/repo, not {world.project}; it is not reused"
        )
        checks = {
            "refused": message == expected,
            "mirror_unchanged": (refs(mirror), git("config", "remote.origin.url", cwd=mirror)) == before,
            "no_worktree": not list(world.execution.path("worktree").iterdir()),
            "no_record": workspaces.recorded(world.execution, "t1") is None,
            "credential_not_disclosed": FIXTURE_PASSWORD not in message,
        }
        corrected = workspaces.prepare(world.execution, world.request(), reuse=False)
        checks["correction_needs_new_request"] = corrected.mirror != mirror and corrected.base_commit == world.head()
        git("checkout", "-q", "-b", "side", cwd=world.work)
        side = commit(world.work, "side")
        git("push", "-q", str(world.origin), "side", cwd=world.work)
        stale = _refusal(world.execution, world.request("t2", minimum=side), reuse=False)
        checks["stale_base_refused"] = stale == f"base dev at {world.head()} does not contain {side}; it is stale"
        checks["stale_base_not_recorded"] = workspaces.recorded(world.execution, "t2") is None
        seconds = round(corrected.workspace_prepare_seconds, 3)
    return {
        "then": "a cached repository with a mismatched origin cannot be reused based on its folder name alone",
        "passed": all(checks.values()),
        **checks,
        "workspace_prepare_seconds": seconds,
    }


def case_c():
    with tempfile.TemporaryDirectory() as root:
        world = World(Path(root))
        first = workspaces.prepare(world.execution, world.request("t1"))
        kept = workspaces.recorded(world.execution, "t1")
        commit(world.work, "unseen")
        git("push", "-q", str(world.origin), "dev", cwd=world.work)
        before = refs(first.mirror)
        world.origin.rename(world.root / "gone.git")
        failed = _refusal(world.execution, world.request("t2"))
        checks = {
            "fetch_failure_refused": failed
            == f"fetch of {world.project} failed; the cached base is unverified, so work does not start",
            "prior_cache_refs_untouched": refs(first.mirror) == before,
            "no_work_from_unverified_base": workspaces.recorded(world.execution, "t2") is None,
            "earlier_workspace_survived": workspaces.recorded(world.execution, "t1") == kept and first.path.is_dir(),
        }
        (world.root / "gone.git").rename(world.origin)
        recovered = workspaces.prepare(world.execution, world.request("t2", generation=2))
        replayed = workspaces.prepare(world.execution, world.request("t2", generation=2))
        record = workspaces.recorded(world.execution, "t2")
        fenced = _refusal(world.execution, replace(world.request("t2"), generation=1))
        checks["recovered_at_fresh_base"] = recovered.base_commit == world.head()
        checks["replay_without_duplicate"] = (
            replayed == recovered and len(list(world.execution.path("worktree").iterdir())) == 2
        )
        checks["older_generation_fenced"] = (
            fenced == "task t2 generation 1 is older than its recorded generation 2"
            and workspaces.recorded(world.execution, "t2") == record
        )
        seconds = round(recovered.workspace_prepare_seconds, 3)
    return {
        "then": (
            "a fetch failure leaves the prior cache untouched and does not start work from an unverified stale base"
        ),
        "passed": all(checks.values()),
        **checks,
        "workspace_prepare_seconds": seconds,
    }
