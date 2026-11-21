import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from contextlib import redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hooks.context import blocker_window
from scripts import ci_prepush
from scripts.ci_lab import head_notice
from scripts.ci_lab.remote import checkout
from scripts.gates import entry as gate_entry
from scripts.swarm import continuation, git_https, naming, publish_queue, stack
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, SwarmError


class PublicationRefusal(SwarmError):
    def __init__(self, repository: Path, branch: str, reason: str) -> None:
        super().__init__(reason)
        self.repository = str(repository)
        self.branch = branch


LAB = "lab"
GRADE_REFS = "refs/grade/"
GRADE_URL = re.compile(r"lab grade: (\S+)")
GREEN = "success"
LAB_WAIT_S = 1800
LAB_POLL_S = 15
LAB_READ_TIMEOUT_S = 10


@dataclass(frozen=True)
class LabRoute:
    """A publish through the lab: the task whose grade ref it pushes, and whether origin follows without the grade."""

    task: str
    direct: bool


def route_flags(lab: LabRoute | None) -> list[str]:
    """The publish flags that repeat a lab route, empty for a publish straight to origin."""
    if lab is None:
        return []
    return ["--via", LAB, "--direct"] if lab.direct else ["--via", LAB]


def read_grade(url: str) -> dict[str, Any]:
    """Return the lab's answer for one head's grades, as GET /grades/<sha> gives it."""
    with urllib.request.urlopen(url, timeout=LAB_READ_TIMEOUT_S) as answer:
        return json.load(answer)


def lab_grades(url: str) -> list[dict[str, Any]]:
    """Return the grades the lab holds at url; an answer that cannot be read refuses the publish."""
    try:
        return read_grade(url)["grades"]
    except (OSError, ValueError, KeyError) as exc:
        raise SwarmError(f"the lab grade {url} could not be read, so origin was not pushed: {exc!r}") from exc


def lab_conclusion(url: str) -> str:
    """Wait for the newest grade at url to conclude and return its conclusion; refuse once LAB_WAIT_S has passed."""
    deadline = time.monotonic() + LAB_WAIT_S
    while not (grades := lab_grades(url)) or not grades[-1]["conclusion"]:
        if time.monotonic() >= deadline:
            raise SwarmError(f"the lab grade {url} has not concluded after {LAB_WAIT_S} s; origin was not pushed")
        time.sleep(LAB_POLL_S)
    return grades[-1]["conclusion"]


def lab_push(root: Path, task: str, branch: str) -> str:
    """Push HEAD to the task's grade ref on the lab remote with the branch the relay writes, show the lab's answer and
    return the grade url it names."""
    if not checkout.carry(checkout.at(root), os.environ):
        raise SwarmError(f"publish --via {LAB} needs {checkout.URL_ENV} naming the lab remote of this repository")
    done = git_https.run(
        ["git", "-c", "push.negotiate=true", "push", "-o", f"branch={branch}", LAB, f"HEAD:{GRADE_REFS}{task}"],
        cwd=root,
    )
    sys.stderr.write(done.stderr)
    found = GRADE_URL.search(done.stderr)
    if done.returncode or not found:
        raise SwarmError(f"the lab push named no grade url; origin was not pushed: {done.stderr.strip()}")
    return found[1]


def through_lab(root: Path, route: LabRoute, fields: dict[str, str]) -> None:
    """Grade HEAD on the lab before origin: push it there and, unless the route is direct, wait for a green grade."""
    url = fields["lab_grade"] = lab_push(root, route.task, fields["branch"])
    verdict = GREEN if route.direct else lab_conclusion(url)
    if verdict != GREEN:
        raise SwarmError(f"the lab grade {url} concluded {verdict}; origin was not pushed")


def execute(argv: list[str], root: Path) -> str:
    done = git_https.run(argv, cwd=root)
    if done.returncode:
        raise SwarmError(f"{shlex.join(argv)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def git(root: Path, *args: str) -> str:
    output = execute(["git", *args], root)
    return output if "-z" in args else output.strip()


def task_row(slug: str, agent: AgentRecord, ledger: LedgerClient) -> dict:
    task = ledger.task(slug, agent.task) or {}
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


def mapped_branch(repo: Path, task: dict) -> str | None:
    branches = task.get("repository_branches", {})
    if (
        task.get("pr_url")
        and len({value for key, value in branches.items() if repository_name(key) == repository_name(str(repo))}) > 1
    ):
        head = publish_queue.open_head(git(repo, "remote", "get-url", "origin"), task["pr_url"])
        if head:
            return head
    branch = branches.get(str(repo))
    if branch is None:
        branch = next(
            (value for key, value in branches.items() if repository_name(key) == repository_name(str(repo))), None
        )
    return branch


def refusal_branch(repo: Path, task: dict) -> str:
    branch = mapped_branch(repo, task)
    return task.get("branch", "") if branch is None else branch


def recorded_branch(repo: Path, task: dict) -> str:
    remote = task.get("branch_repo")
    if remote and stack.repo_key(remote) != stack.repo_key(git(repo, "remote", "get-url", "origin")):
        return ""
    if not remote and len(task.get("repositories", [])) > 1:
        return ""
    return task.get("branch") or ""


def repository_task(repo: Path, task: dict) -> dict:
    branch = mapped_branch(repo, task)
    if branch is None:
        branch = recorded_branch(repo, task)
    return {**task, "branch": require_branch(branch) if branch else ""}


def continuation_task(repo: Path, task: dict, scoped: dict, branch: str) -> dict:
    mapped, recorded = scoped["branch"], recorded_branch(repo, task)
    if branch == mapped or recorded in ("", mapped):
        return scoped
    heads = {name for name in (mapped, recorded) if git(repo, "ls-remote", "origin", f"refs/heads/{name}")}
    if mapped in heads or recorded not in heads:
        return scoped
    return {**scoped, "branch": recorded}


def publishing_root(repo: str, task: dict, agent: AgentRecord) -> tuple[Path | None, Path, dict]:
    current = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    root = None
    if current.returncode == 0 and (Path(current.stdout.strip()) / ".git").is_file():
        root = Path(current.stdout.strip())
        primary = repository_root(root)
    else:
        repositories = task.get("repositories") or [repo]
        if len(repositories) != 1:
            raise SwarmError("publish from the task worktree to select one of its repositories")
        primary = local_repository(repositories[0], repo)
    try:
        declared = task.get("repositories")
        if root and declared and repository_name(str(primary)) not in {repository_name(value) for value in declared}:
            raise SwarmError(f"current worktree repository {primary} is not in the task repositories")
        scoped = repository_task(primary, task)
        if root is None:
            return existing(primary, scoped, agent), primary, scoped
        branch = require_branch(git(root, "branch", "--show-current"))
        scoped = continuation_task(primary, task, scoped, branch)
        own = re.fullmatch(rf"{re.escape(naming.plain(agent.name))}(?:-\d+)?", branch)
        if not (own or branch == scoped.get("branch")):
            raise SwarmError(f"current worktree {root} does not belong to this task")
        return root, primary, scoped
    except SwarmError as exc:
        raise PublicationRefusal(primary, refusal_branch(primary, task), str(exc)) from exc


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


def merged_record(repo: Path, task: dict[str, str]) -> bool:
    if not task.get("branch") or not task.get("pr_url"):
        return False
    if stack.repo_key(task["pr_url"].split("/pull/")[0]) != stack.repo_key(git(repo, "remote", "get-url", "origin")):
        return False
    pull = stack.change(task["pr_url"], f"cannot read pull request {task['pr_url']}")
    return pull.merged and pull.head_branch == task["branch"]


def open_pull_head(root: Path, task: dict, branch: str, remote: str) -> str:
    url = task.get("pr_url", "")
    head = publish_queue.open_head(remote, url)
    if head and head != branch:
        git(root, "fetch", "origin", head)
        if not descends(root, head):
            raise SwarmError(
                f"open pull request {url} delivers branch {head}, but worktree {root} on branch {branch} "
                f"does not descend from it; continue from origin/{head} or close that pull request"
            )
    return head


def prepare(slug: str, agent: AgentRecord, repo: str, ledger: LedgerClient) -> dict:
    task = task_row(slug, agent, ledger)
    fields, cleared = [], {}
    repositories = dict.fromkeys(local_repository(value, repo) for value in task.get("repositories") or [repo])
    for repository in repositories:
        scoped = task
        try:
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
                if scoped.get("branch") and (scoped.get("pr_url") or remote_head(repository, scoped["branch"])):
                    command.extend(["--from", f"origin/{scoped['branch']}"])
                output = execute(command, repository)
                root = Path(output.splitlines()[-1])
            checkout = checkout_fields(root)
            head = open_pull_head(root, scoped, checkout["branch"], checkout["branch_repo"])
            delivery = head or target_branch(root, scoped, checkout["branch"], scoped if retired else task)
            if delivery != checkout["branch"]:
                git(root, "branch", f"--set-upstream-to=origin/{delivery}")
            checkout["branch"] = head or checkout["branch"]
            fields.append({**checkout, "repository": str(repository)})
        except SwarmError as exc:
            raise PublicationRefusal(repository, refusal_branch(repository, scoped), str(exc)) from exc
    registered = task
    for entry in fields:
        registered = drop_repository(registered, Path(entry["repository"]))
    update = branch_record(fields[-1], Path(fields[-1]["repository"]), registered)
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
        remote = git(root, "remote", "get-url", "origin")
        delivered = publish_queue.open_head(remote, task.get("pr_url", ""))
        if delivered == recorded and descends(root, recorded):
            return recorded
        if not delivered and cut_from(root, recorded) and settled(task):
            return branch
        raise SwarmError(f"current worktree {root} branch {branch} differs from registered branch {recorded}")
    return branch


def cut_from(root: Path, recorded: str) -> bool:
    head = remote_head(root, recorded)
    if not head:
        return False
    return git(root, "merge-base", head, "HEAD") == head


def drop_repository(task: dict, repository: Path) -> dict:
    name = repository_name(str(repository))
    branches = task.get("repository_branches", {})
    return {
        **task,
        "repository_branches": {key: value for key, value in branches.items() if repository_name(key) != name},
    }


def settled(task: dict[str, object]) -> bool:
    url = str(task.get("pr_url") or "")
    if not url:
        return True
    return stack.change(url, f"cannot read pull request {url}").state == "closed"


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


def lab_repository(remote: str) -> str:
    """Return the repository a lab head notice names for an origin remote, in the form GitHub deliveries give it."""
    return f"https://{stack.repo_key(remote)}"


def notify_lab(root: Path, fields: dict[str, str], head: str) -> None:
    """Send the lab receiver the signed notice of the head just pushed, based on its merge base with dev, and record
    the receiver's answer as lab_notice; a notice that is not configured is not sent, and one that fails leaves the
    push standing."""
    base = subprocess.run(["git", "merge-base", head, "origin/dev"], cwd=root, capture_output=True, text=True)
    status = head_notice.send(os.environ, lab_repository(fields["branch_repo"]), head, base.stdout.strip())
    if status is not None:
        fields["lab_notice"] = str(status)


def grade_and_push(
    slug: str, root: Path, primary: Path, fields: dict[str, str], head: str, route: LabRoute | None = None
) -> None:
    held = blocker_window.held_push(str(root), fields["branch"], slug)
    if held:
        raise SwarmError(held)
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
    if route is not None:
        through_lab(root, route, fields)
    git(root, "push", "-u", "origin", f"HEAD:{ref}")
    notify_lab(root, fields, head)


def publish(
    slug: str,
    agent: AgentRecord,
    repo: str,
    ledger: LedgerClient,
    message: str,
    files: list[str],
    lab: LabRoute | None = None,
) -> dict:
    task = task_row(slug, agent, ledger)
    primary, scoped, fields = Path(repo), task, {}
    retry = ""
    try:
        root, primary, scoped = publishing_root(repo, task, agent)
        if root is None:
            raise SwarmError(f"task worktree missing; Next: agentihooks swarm {shlex.quote(slug)} prepare")
        fields = checkout_fields(root)
        selected = selected_files(root, files)
        retry = shlex.join(
            ["agentihooks", "swarm", slug, "publish", "--message", message, *route_flags(lab), "--", *files]
        )
        fields["branch"] = delivery_branch(root, scoped, fields["branch"])
        ledger.update_task(
            slug, agent.task, branch_record(fields, primary, drop_repository(task, primary)), by=agent.name
        )
        stage_files(root, selected)
        changed = bool(git(root, "diff", "--cached", "--name-only", "-z"))
        if changed:
            check_build(root)
            git(root, "commit", "-m", message)
        head = git(root, "rev-parse", "HEAD")
        if changed or remote_head(root, fields["branch"]) != head:
            grade_and_push(slug, root, primary, fields, head, lab)
        else:
            fields["unchanged"] = (
                "no changes in the listed files and the remote branch already points at this head; nothing pushed"
            )
    except SwarmError as exc:
        detail = str(exc)
        if fields.get("gate") == "AgentiHooks touched Python fallback":
            detail = f"AgentiHooks touched Python fallback for {root}: {detail}"
        branch = exc.branch if isinstance(exc, PublicationRefusal) else scoped.get("branch", "")
        repository = Path(exc.repository) if isinstance(exc, PublicationRefusal) else primary
        raise PublicationRefusal(repository, branch, f"{detail}; Next: {retry}" if retry else detail) from exc
    return {**fields, "head": head}
