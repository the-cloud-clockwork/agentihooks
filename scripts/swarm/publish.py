import re
import shlex
import subprocess
import sys
from pathlib import Path

from scripts import ci_prepush
from scripts.swarm import naming, stack
from scripts.swarm.store import AgentRecord, SwarmError


def execute(argv: list[str], root: Path) -> str:
    done = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    if done.returncode:
        raise SwarmError(f"{shlex.join(argv)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def git(root: Path, *args: str) -> str:
    return execute(["git", *args], root)


def task_row(slug: str, agent: AgentRecord, ledger) -> dict:
    task = next((row for row in ledger.tasks(slug) if row.get("id") == agent.task), {})
    if task.get("claimed_by") != agent.name or task.get("state") not in ("claimed", "pr"):
        raise SwarmError("publication requires this agent's claimed task")
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
        if branch == task.get("branch"):
            recorded.append(path)
        elif own.fullmatch(branch):
            found.append(path)
    found = recorded or found
    if len(found) > 1:
        raise SwarmError("multiple task worktrees match; record the task branch before publishing")
    return found[0] if found else None


def checkout_fields(root: Path) -> dict:
    branch = git(root, "branch", "--show-current")
    if not branch or branch in ("dev", "main", "master", "v1"):
        raise SwarmError("publication requires a task branch outside the protected branches")
    return {
        "worktree": str(root),
        "branch": branch,
        "branch_repo": stack.public_url(git(root, "remote", "get-url", "origin")),
    }


def prepare(slug: str, agent: AgentRecord, repo: str, ledger) -> dict:
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


def publish(slug: str, agent: AgentRecord, repo: str, ledger, message: str, files: list[str]) -> dict:
    task = task_row(slug, agent, ledger)
    root = existing(Path(repo), task, agent)
    if root is None:
        raise SwarmError(f"task worktree missing; Next: agentihooks swarm {shlex.quote(slug)} prepare")
    fields = checkout_fields(root)
    selected = selected_files(root, files)
    retry = shlex.join(["agentihooks", "swarm", slug, "publish", "--message", message, "--", *files])
    try:
        if selected:
            git(root, "--literal-pathspecs", "add", "--", *selected)
        if git(root, "diff", "--cached", "--name-only"):
            git(root, "commit", "-m", message)
        head = git(root, "rev-parse", "HEAD")
        execute([sys.executable, "-m", "scripts.ci_prepush"], root)
        if not ci_prepush.passed(root) or git(root, "rev-parse", "HEAD") != head:
            raise SwarmError("the committed HEAD has no passing prepush evidence")
        git(root, "push", "-u", "origin", f"HEAD:{fields['branch']}")
    except SwarmError as exc:
        raise SwarmError(f"{exc}; Next: {retry}") from exc
    ledger.update_task(slug, agent.task, {key: fields[key] for key in ("branch", "branch_repo")}, by=agent.name)
    return {**fields, "head": head}
