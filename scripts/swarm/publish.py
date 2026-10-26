import io
import json
import re
import shlex
import subprocess
import sys
from contextlib import redirect_stderr
from pathlib import Path

from scripts import ci_prepush
from scripts.gates import entry as gate_entry
from scripts.swarm import continuation, naming, publish_queue, stack
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, SwarmError


def execute(argv: list[str], root: Path) -> str:
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    if done.returncode:
        raise SwarmError(f"{shlex.join(argv)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def git(root: Path, *args: str) -> str:
    output = execute(["git", *args], root)
    return output if "-z" in args else output.strip()


def task_row(slug: str, agent: AgentRecord, ledger: LedgerClient) -> dict:
    task = next((row for row in ledger.tasks(slug) if row.get("id") == agent.task), {})
    if task.get("claimed_by") != agent.name or task.get("state") not in ("claimed", "pr"):
        raise SwarmError("publication requires this agent's claimed task")
    if task.get("branch"):
        require_branch(task["branch"])
    return task


def existing(repo: Path, task: dict, agent: AgentRecord) -> Path | None:
    own = re.compile(rf"{re.escape(naming.plain(agent.name))}(?:-\d+)?")
    found, recorded = [], []
    for entry in git(repo, "worktree", "list", "--porcelain").split("\n\n"):
        lines = entry.splitlines()
        path = Path(lines[0].removeprefix("worktree "))
        branch = next((line.removeprefix("branch refs/heads/") for line in lines if line.startswith("branch ")), "")
        if not (path / ".git").is_file():
            continue
        if task.get("branch") and branch == task["branch"]:
            recorded.append(path)
        elif own.fullmatch(branch):
            found.append(path)
    found = recorded or found
    if len(found) > 1:
        raise SwarmError("multiple task worktrees match; record the task branch before publishing")
    return found[0] if found else None


def require_branch(branch: str) -> str:
    if not branch or branch in ("dev", "main", "master", "v1"):
        raise SwarmError("publication requires a task branch outside the protected branches")
    return branch


def checkout_fields(root: Path) -> dict:
    branch = require_branch(git(root, "branch", "--show-current"))
    return {
        "worktree": str(root),
        "branch": branch,
        "branch_repo": stack.public_url(git(root, "remote", "get-url", "origin")),
    }


def branch_record(fields: dict, repository: Path, task: dict) -> dict:
    return {
        "branch": fields["branch"],
        "branch_repo": fields["branch_repo"],
        "repository_branches": {**task.get("repository_branches", {}), str(repository): fields["branch"]},
    }


def repository_root(repo: Path) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def repository_name(value: str) -> str:
    return stack.repo_key(value).rpartition("/")[2].lower()


def local_repository(value: str, repo: str) -> Path:
    if Path(value).is_dir():
        return repository_root(Path(value))
    from scripts.hive import daemon

    name = repository_name(value)
    candidates = [found.parent for found in Path(repo).parent.glob("*/.git")] + daemon._checkouts()
    found = {
        repository_root(path) for path in candidates if (path / ".git").is_dir() and repository_name(str(path)) == name
    }
    if len(found) != 1:
        raise SwarmError(f"repository {value} has {len(found)} checkouts named {name} on this host")
    return found.pop()


def repository_task(repo: Path, task: dict) -> dict:
    branches = task.get("repository_branches", {})
    branch = branches.get(str(repo))
    if branch is None:
        branch = next(
            (value for key, value in branches.items() if repository_name(key) == repository_name(str(repo))), None
        )
    if branch is None:
        branch = task.get("branch", "")
        remote = task.get("branch_repo")
        if remote and stack.repo_key(remote) != stack.repo_key(git(repo, "remote", "get-url", "origin")):
            branch = ""
        elif not remote and len(task.get("repositories", [])) > 1:
            branch = ""
    return {**task, "branch": require_branch(branch) if branch else ""}


def publishing_root(repo: str, task: dict, agent: AgentRecord) -> tuple[Path | None, Path, dict]:
    current = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if current.returncode == 0 and (Path(current.stdout.strip()) / ".git").is_file():
        root = Path(current.stdout.strip())
        primary = repository_root(root)
        declared = task.get("repositories")
        if declared and repository_name(str(primary)) not in {repository_name(value) for value in declared}:
            raise SwarmError(f"current worktree repository {primary} is not in the task repositories")
        scoped = repository_task(primary, task)
        branch = require_branch(git(root, "branch", "--show-current"))
        own = re.fullmatch(rf"{re.escape(naming.plain(agent.name))}(?:-\d+)?", branch)
        if not (own or branch == scoped.get("branch")):
            raise SwarmError(f"current worktree {root} does not belong to this task")
        return root, primary, scoped
    repositories = task.get("repositories") or [repo]
    if len(repositories) != 1:
        raise SwarmError("publish from the task worktree to select one of its repositories")
    primary = local_repository(repositories[0], repo)
    scoped = repository_task(primary, task)
    return existing(primary, scoped, agent), primary, scoped


def pushed_branches(repo: str, task: dict, agent: AgentRecord) -> dict:
    record, resolved = {}, []
    for value in task.get("repositories") or []:
        try:
            resolved.append(local_repository(value, repo))
        except SwarmError:
            continue
    candidates = resolved or ([repository_root(Path(repo))] if Path(repo).is_dir() else [])
    for repository in dict.fromkeys(candidates):
        scoped = repository_task(repository, task)
        root = existing(repository, scoped, agent)
        if root and stack.pushed(root, scoped["branch"]):
            record = branch_record(checkout_fields(root), repository, {**task, **record})
    return record


def merged_record(repo: Path, task: dict) -> bool:
    if not task.get("branch") or not task.get("pr_url"):
        return False
    if stack.repo_key(task["pr_url"].split("/pull/")[0]) != stack.repo_key(git(repo, "remote", "get-url", "origin")):
        return False
    pull = json.loads(execute(["gh", "pr", "view", task["pr_url"], "--json", "state,headRefName"], Path(repo)))
    return pull["state"] == "MERGED" and pull["headRefName"] == task["branch"]


def prepare(slug: str, agent: AgentRecord, repo: str, ledger: LedgerClient) -> dict:
    task = task_row(slug, agent, ledger)
    fields, cleared = [], {}
    repositories = dict.fromkeys(local_repository(value, repo) for value in task.get("repositories") or [repo])
    for repository in repositories:
        scoped = repository_task(repository, task)
        retired = merged_record(repository, scoped)
        if retired:
            cleared = {"pr_url": ""}
            hints = {
                key: {
                    name: value
                    for name, value in (task.get(key) or {}).items()
                    if name != "continue_from" or value != f"origin/{scoped['branch']}"
                }
                for key in ("handoff_envelope", "reclaim")
            }
            scoped = {**scoped, **cleared, **hints, "branch": ""}
        root = None if retired else existing(repository, scoped, agent)
        if root is None:
            command = ["bash", str(stack.WT_SCRIPT), "new", "--repo", str(repository)]
            if scoped.get("branch"):
                command.extend(["--from", f"origin/{scoped['branch']}"])
            output = execute(command, repository)
            root = Path(output.splitlines()[-1])
        checkout = checkout_fields(root)
        delivery = target_branch(root, scoped, checkout["branch"], scoped if retired else task)
        if delivery != checkout["branch"]:
            git(root, "branch", f"--set-upstream-to=origin/{delivery}")
        fields.append({**checkout, "repository": str(repository)})
    update = branch_record(fields[-1], Path(fields[-1]["repository"]), task)
    update["repository_branches"].update({entry["repository"]: entry["branch"] for entry in fields})
    ledger.update_task(slug, agent.task, {**cleared, **update}, by=agent.name)
    result = fields[0] if len(fields) == 1 else {"worktrees": fields}
    return {**result, "repository_source": "task" if task.get("repositories") else "swarm default"}


def staged_changes(root: Path) -> set[str]:
    changed = set(git(root, "diff", "--cached", "--name-only", "--no-renames", "-z").split("\0"))
    merge_head = root / git(root, "rev-parse", "--git-path", "MERGE_HEAD")
    if merge_head.is_file():
        for parent in merge_head.read_text().splitlines():
            on_dev = subprocess.run(
                ["git", "merge-base", "--is-ancestor", parent, "origin/dev"], cwd=root, capture_output=True
            )
            if on_dev.returncode == 0:
                changed &= set(git(root, "diff", "--cached", "--name-only", "--no-renames", "-z", parent).split("\0"))
    return changed


def selected_files(root: Path, files: list[str]) -> list[str]:
    selected = []
    for name in files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or (root / path).is_dir():
            raise SwarmError("publish accepts explicit repository relative file names only")
        selected.append(path.as_posix())
    changed = set(git(root, "diff", "--name-only", "--no-renames", "-z").split("\0")) | staged_changes(root)
    unrelated = changed - set(selected) - {""}
    if unrelated:
        raise SwarmError(
            f"unrelated tracked changes remain: {', '.join(sorted(unrelated))}; Next: git -C {shlex.quote(str(root))} status --short"
        )
    return selected


def committed_file(root: Path, name: str, revision: str) -> bool:
    done = subprocess.run(["git", "cat-file", "-t", f"{revision}:{name}"], cwd=root, capture_output=True, text=True)
    return done.stdout.strip() == "blob"


def stage_files(root: Path, selected: list[str]) -> None:
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    missing = {
        name
        for name in selected
        if not (root / name).exists() and not (root / name).is_symlink() and name not in tracked
    }
    unknown = {name for name in missing if not any(committed_file(root, name, ref) for ref in ("HEAD", "HEAD^"))}
    if unknown:
        raise SwarmError(f"selected files do not exist: {', '.join(sorted(unknown))}")
    pending = [name for name in selected if name not in missing]
    if pending:
        git(root, "--literal-pathspecs", "add", "--", *pending)


def check_build(root: Path) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit"}, "cwd": str(root)}
    reason = io.StringIO()
    with redirect_stderr(reason):
        refused = gate_entry.main(["build"], stdin=io.StringIO(json.dumps(payload)))
    if refused:
        raise SwarmError(reason.getvalue().strip())


def tracked(root: Path, record: dict) -> str:
    done = subprocess.run(
        ["git", "rev-parse", "--symbolic-full-name", "@{upstream}"], cwd=root, capture_output=True, text=True
    )
    branch = done.stdout.strip().removeprefix("refs/remotes/origin/")
    recorded = {record.get("branch"), *record.get("repository_branches", {}).values()} - continuation.PROTECTED
    return branch if branch in recorded else ""


def target_branch(root: Path, task: dict, branch: str, record: dict) -> str:
    for candidate in (continuation.retained(task), tracked(root, record), task.get("branch")):
        if candidate:
            if descends(root, candidate):
                return candidate
    return branch


def descends(root: Path, branch: str) -> bool:
    ref = f"refs/remotes/origin/{branch}"
    return not subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, "HEAD"], cwd=root, capture_output=True
    ).returncode


def delivery_branch(root: Path, task: dict, branch: str) -> str:
    recorded = task.get("branch")
    if recorded and branch != recorded:
        raise SwarmError(f"current worktree {root} branch {branch} differs from registered branch {recorded}")
    return branch


def gate_python(repo: Path) -> str:
    venv = repo.parent / ".venv" / "bin" / "python"
    for python in (venv, Path(sys.executable)):
        if python.is_file() and not subprocess.run([python, "-I", "-c", "import ruff"], capture_output=True).returncode:
            return str(python)
    raise SwarmError(f"no python with ruff for the prepush gate; install the dev extra into {venv}")


def native_gate(python: str, root: Path) -> list[str]:
    if (root / "scripts" / "__init__.py").is_file():
        return [python, "-m", "scripts.ci_prepush"]
    code = (
        "import runpy, sys, types; "
        "package = types.ModuleType('scripts'); package.__path__ = [sys.argv[1]]; "
        "sys.modules['scripts'] = package; runpy.run_module('scripts.ci_prepush', run_name='__main__')"
    )
    return [python, "-c", code, str(root / "scripts")]


def remote_head(root: Path, branch: str) -> str:
    listed = git(root, "ls-remote", "origin", f"refs/heads/{branch}")
    return listed.split()[0] if listed else ""


def grade_and_push(root: Path, primary: Path, fields: dict, head: str) -> None:
    python = gate_python(primary)
    source = root / "scripts" / "ci_prepush"
    if (source / "__main__.py").is_file() or source.with_suffix(".py").is_file():
        fields["gate"] = str(source if source.is_dir() else source.with_suffix(".py"))
        execute(native_gate(python, root), root)
    else:
        fields["gate"] = "AgentiHooks touched Python fallback"
        execute([python, "-I", str(Path(__file__).with_name("publish_gate.py"))], root)
    if not ci_prepush.passed(root) or git(root, "rev-parse", "HEAD") != head:
        raise SwarmError("the committed HEAD has no passing prepush evidence")
    ref = f"refs/heads/{fields['branch']}"
    publish_queue.require_unqueued(git(root, "remote", "get-url", "--push", "origin"), fields["branch"])
    git(root, "push", "-u", "origin", f"HEAD:{ref}")


def publish(slug: str, agent: AgentRecord, repo: str, ledger: LedgerClient, message: str, files: list[str]) -> dict:
    task = task_row(slug, agent, ledger)
    root, primary, scoped = publishing_root(repo, task, agent)
    if root is None:
        raise SwarmError(f"task worktree missing; Next: agentihooks swarm {shlex.quote(slug)} prepare")
    fields = checkout_fields(root)
    selected = selected_files(root, files)
    retry = shlex.join(["agentihooks", "swarm", slug, "publish", "--message", message, "--", *files])
    try:
        fields["branch"] = delivery_branch(root, scoped, fields["branch"])
        ledger.update_task(slug, agent.task, branch_record(fields, primary, task), by=agent.name)
        stage_files(root, selected)
        changed = bool(git(root, "diff", "--cached", "--name-only", "-z"))
        if changed:
            check_build(root)
            git(root, "commit", "-m", message)
        head = git(root, "rev-parse", "HEAD")
        if changed or remote_head(root, fields["branch"]) != head:
            grade_and_push(root, primary, fields, head)
        else:
            fields["unchanged"] = (
                "no changes in the listed files and the remote branch already points at this head; nothing pushed"
            )
    except SwarmError as exc:
        detail = str(exc)
        if fields.get("gate") == "AgentiHooks touched Python fallback":
            detail = f"AgentiHooks touched Python fallback for {root}: {detail}"
        raise SwarmError(f"{detail}; Next: {retry}") from exc
    return {**fields, "head": head}
