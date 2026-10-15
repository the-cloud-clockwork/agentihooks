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
from scripts.swarm import naming, stack
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


def prepare(slug: str, agent: AgentRecord, repo: str, ledger: LedgerClient) -> dict:
    task = task_row(slug, agent, ledger)
    root = existing(Path(repo), task, agent)
    if root is None:
        command = ["bash", str(stack.WT_SCRIPT), "new", "--repo", repo]
        if task.get("branch"):
            command.extend(["--from", f"origin/{task['branch']}"])
        output = execute(command, Path(repo))
        root = Path(output.splitlines()[-1])
    return checkout_fields(root)


def selected_files(root: Path, files: list[str]) -> list[str]:
    selected = []
    for name in files:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or (root / path).is_dir():
            raise SwarmError("publish accepts explicit repository relative file names only")
        selected.append(path.as_posix())
    changed = git(root, "diff", "--name-only", "-z").split("\0")
    changed += git(root, "diff", "--cached", "--name-only", "-z").split("\0")
    unrelated = set(changed) - set(selected) - {""}
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


def gate_python(repo: Path) -> str:
    venv = repo.parent / ".venv" / "bin" / "python"
    for python in (venv, Path(sys.executable)):
        if python.is_file() and not subprocess.run([python, "-I", "-c", "import ruff"], capture_output=True).returncode:
            return str(python)
    raise SwarmError(f"no python with ruff for the prepush gate; install the dev extra into {venv}")


def publish(slug: str, agent: AgentRecord, repo: str, ledger: LedgerClient, message: str, files: list[str]) -> dict:
    task = task_row(slug, agent, ledger)
    root = existing(Path(repo), task, agent)
    if root is None:
        raise SwarmError(f"task worktree missing; Next: agentihooks swarm {shlex.quote(slug)} prepare")
    fields = checkout_fields(root)
    if task.get("branch"):
        fields["branch"] = task["branch"]
    selected = selected_files(root, files)
    retry = shlex.join(["agentihooks", "swarm", slug, "publish", "--message", message, "--", *files])
    try:
        stage_files(root, selected)
        if git(root, "diff", "--cached", "--name-only", "-z"):
            check_build(root)
            git(root, "commit", "-m", message)
        head = git(root, "rev-parse", "HEAD")
        execute([gate_python(Path(repo)), "-m", "scripts.ci_prepush"], root)
        if not ci_prepush.passed(root) or git(root, "rev-parse", "HEAD") != head:
            raise SwarmError("the committed HEAD has no passing prepush evidence")
        git(root, "push", "-u", "origin", f"HEAD:{fields['branch']}")
    except SwarmError as exc:
        raise SwarmError(f"{exc}; Next: {retry}") from exc
    ledger.update_task(slug, agent.task, {key: fields[key] for key in ("branch", "branch_repo")}, by=agent.name)
    return {**fields, "head": head}
