import tempfile
from dataclasses import replace
from pathlib import Path

from scripts.swarm_v2 import workspaces
from tests.test_swarm_v2_workspaces import World, commit, git, refs

CREDENTIAL_URL = "https://fixture:not-a-secret@example.invalid/other/repo.git"


def _prepared(world: World, workspace: workspaces.Workspace) -> dict:
    return {
        "inside_execution_root": workspace.path.is_relative_to(world.execution.root),
        "mirror_inside_execution_root": workspace.mirror.is_relative_to(world.execution.root),
        "at_fresh_base": git("rev-parse", "HEAD", cwd=workspace.path) == world.head() == workspace.base_commit,
        "branch_from_naming_helper": workspace.branch == "engineer-abc123-0007",
        "project_matches_origin": workspace.project == world.project,
        "record_matches": workspaces.recorded(world.execution, workspace.task)["base_commit"] == world.head(),
        "task_generation": [workspace.task, workspace.generation],
        "workspace_prepare_seconds_measured": workspace.workspace_prepare_seconds >= 0,
    }


def case_a():
    runs = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as root:
            world = World(Path(root))
            workspace = workspaces.prepare(world.execution, world.request())
            run = _prepared(world, workspace)
            replayed = workspaces.prepare(world.execution, world.request())
            run["replay_returns_same_workspace"] = replayed == workspace
            run["worktrees"] = len(list(world.execution.path("worktree").iterdir()))
            run["passed"] = all(v for k, v in run.items() if k != "worktrees") and run["worktrees"] == 1
            run["workspace_prepare_seconds"] = round(workspace.workspace_prepare_seconds, 3)
            runs.append(run)
    return {
        "then": "an agent begins in an isolated worktree tied to the correct project and task generation",
        "passed": all(run["passed"] for run in runs),
        "independent_fixtures": runs,
    }


def case_b():
    with tempfile.TemporaryDirectory() as root:
        world = World(Path(root))
        mirror = workspaces.mirror_path(world.execution, world.project)
        git("clone", "-q", "--bare", f"file://{world.other}", str(mirror))
        git("config", "remote.origin.url", CREDENTIAL_URL, cwd=mirror)
        before = (refs(mirror), git("config", "remote.origin.url", cwd=mirror))
        try:
            workspaces.prepare(world.execution, world.request())
            message = ""
        except workspaces.WorkspaceError as error:
            message = str(error)
        unchanged = (refs(mirror), git("config", "remote.origin.url", cwd=mirror)) == before
        no_work = not list(world.execution.path("worktree").iterdir())
        no_record = workspaces.recorded(world.execution, "t1") is None
        corrected = workspaces.prepare(world.execution, world.request(), reuse=False)
        result = {
            "refused": message.endswith("; it is not reused"),
            "mirror_unchanged": unchanged,
            "no_worktree": no_work,
            "no_record": no_record,
            "credential_not_disclosed": "not-a-secret" not in message,
            "correction_needs_new_request": corrected.mirror != mirror and corrected.base_commit == world.head(),
        }
    return {
        "then": "a cached repository with a mismatched origin cannot be reused based on its folder name alone",
        "passed": all(result.values()),
        **result,
    }


def case_c():
    with tempfile.TemporaryDirectory() as root:
        world = World(Path(root))
        first = workspaces.prepare(world.execution, world.request("t1"))
        commit(world.work, "unseen")
        git("push", "-q", str(world.origin), "dev", cwd=world.work)
        before = refs(first.mirror)
        world.origin.rename(world.root / "gone.git")
        try:
            workspaces.prepare(world.execution, world.request("t2"))
            refused = False
        except workspaces.WorkspaceError:
            refused = True
        untouched = refs(first.mirror) == before
        no_stale_start = workspaces.recorded(world.execution, "t2") is None
        (world.root / "gone.git").rename(world.origin)
        recovered = workspaces.prepare(world.execution, world.request("t2", generation=2))
        replayed = workspaces.prepare(world.execution, world.request("t2", generation=2))
        record = workspaces.recorded(world.execution, "t2")
        try:
            workspaces.prepare(world.execution, replace(world.request("t2"), generation=1))
            fenced = False
        except workspaces.WorkspaceError:
            fenced = workspaces.recorded(world.execution, "t2") == record
        result = {
            "fetch_failure_refused": refused,
            "prior_cache_refs_untouched": untouched,
            "no_work_from_unverified_base": no_stale_start,
            "recovered_at_fresh_base": recovered.base_commit == world.head(),
            "replay_without_duplicate": replayed == recovered
            and len(list(world.execution.path("worktree").iterdir())) == 2,
            "older_generation_fenced": fenced,
        }
    return {
        "then": "a fetch failure leaves the prior cache untouched and does not start work from an unverified stale base",
        "passed": all(result.values()),
        **result,
    }
