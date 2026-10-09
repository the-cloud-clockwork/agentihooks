"""The Stop gate that keeps a swarm worker's work on origin: it pushes the task branch itself, then refuses the stop over
uncommitted changes, a push origin refused, or pushed work with no pull request and no ledger line since the push."""

import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from hooks.config import CONDITIONS_TIMEOUT_SEC
from scripts.gates.base import Decision
from scripts.swarm.naming import lane_of, plain
from scripts.swarm_ledger import ledger_kinds

WORKERS = frozenset({"eng", "ci"})
TEMPLATE = (
    "You cannot leave uncommitted or unpushed changes. Commit them now. If the task is done, open the pull request. "
    "If not, update the issue and the pull request and record progress on the ledger."
)
SENDER = "swarm"
SETTLED = "a later stop passed with the work committed, on origin and recorded"
PUSHED = "pushed"
GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
GIT_TIMEOUT_S = 60
# Under the Stop condition's kill, with room for the pushes: a gate still running at the kill lets the stop through.
GATE_TIMEOUT_S = CONDITIONS_TIMEOUT_SEC - 2
PREPUSH = Path("scripts") / "ci_prepush" / "__init__.py"
GATE_FAILED = (
    "The pre push gate failed in {path}, so the stop hook did not push it. "
    "Run python -m scripts.ci_prepush there, fix what fails and commit."
)
GATE_SLOW = (
    "The pre push gate did not finish in {seconds:g} s in {path}, so the stop hook did not push it. "
    "Run python -m scripts.ci_prepush there and commit."
)


@dataclass(frozen=True)
class Tree:
    path: Path
    branch: str
    dirty: bool
    unpushed: int
    own: int


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)


def count(path, *args):
    done = git(path, "rev-list", "--count", *args)
    return int(done.stdout) if done.returncode == 0 else 0


def inspect(path, own):
    """The worktree's state, or None when it is not on one of the agent's own branches."""
    branch = git(path, "branch", "--show-current").stdout.strip()
    if not own.fullmatch(branch):
        return None
    return Tree(
        path,
        branch,
        bool(git(path, "status", "--porcelain").stdout.strip()),
        count(path, "HEAD", "--not", "--remotes=origin"),
        count(path, "HEAD", "--not", f"--exclude=origin/{branch}", "--remotes=origin"),
    )


def trees(root, name):
    own = re.compile(rf"{re.escape(plain(name))}(?:-\d+)?")
    found = (inspect(path, own) for path in sorted(Path(root).glob("*/*")) if own.fullmatch(path.name))
    return [tree for tree in found if tree is not None]


def gate_refusal(tree):
    """Why the pre push gate keeps HEAD off origin, or None: a repo without one passes, a stamped HEAD is not rerun."""
    if not (tree.path / PREPUSH).is_file():
        return None
    from scripts.ci_prepush import passed

    if passed(tree.path):
        return None
    gate = subprocess.Popen(
        [sys.executable, "-m", "scripts.ci_prepush"],
        cwd=tree.path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = gate.wait(GATE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        os.killpg(gate.pid, signal.SIGKILL)
        gate.wait()
        return GATE_SLOW.format(path=tree.path, seconds=GATE_TIMEOUT_S)
    return None if code == 0 else GATE_FAILED.format(path=tree.path)


def push(tree):
    ref = f"refs/heads/{tree.branch}"
    return git(tree.path, "push", "--set-upstream", "origin", f"{ref}:{ref}").returncode == 0


def record_text(remote, branch, head):
    found = GITHUB_RE.search(remote)
    if not found:
        return f"The stop hook pushed the task branch {branch} to origin"
    web = f"https://github.com/{found.group(1)}/{found.group(2)}"
    return f"The stop hook pushed the task branch {web}/tree/{branch} with its head at {web}/commit/{head}"


def spoke_since(events, name, at):
    return any(event.get("by") == name and event["at"] > at for event in events)


def read(ledger, slug):
    """The ledger document, or None while the ledger is unreachable: the push and the dirty check still run."""
    from scripts.swarm.store import SwarmError

    try:
        return ledger.state(slug)
    except SwarmError:
        return None


def default_root():
    return os.environ.get("WORKTREE_ROOT") or Path.home() / "dev" / "worktrees"


def is_notice(item):
    return item.sender == SENDER and item.text == TEMPLATE


def left(name, exit_text):
    """The close reason of a notice whose agent has gone, naming whether its branch reached origin."""
    found = trees(default_root(), name)
    if not found:
        return f"{name} {exit_text}; its worktree was not found, so whether its branch was pushed is unknown"
    held = [tree.branch for tree in found if tree.dirty or tree.unpushed]
    branches = ", ".join(held or [tree.branch for tree in found])
    return f"{name} {exit_text}; its branch {branches} was {'not ' if held else ''}pushed"


class PushStop:
    name = "push-stop"
    default_mode = "enforce"

    def __init__(self, connect=None, ledger=None, root=None, now=None):
        self._connect, self._ledger, self._root, self._now = connect, ledger, root, now

    def matches(self, call):
        return not call.tool

    def decide(self, call, who, state):
        if not (who.pinned and who.task) or lane_of(who.name) not in WORKERS:
            return Decision()
        ledger = self.ledger()
        doc = read(ledger, who.swarm)
        task = next((t for t in doc["tasks"] if t.get("id") == who.task), None) if doc else {}
        if task is None or ledger_kinds.kind(task) == "plan":
            return Decision()
        store, owed, failed = self.connect(), False, []
        for tree in trees(self.root(), who.name):
            if tree.unpushed and (refused := gate_refusal(tree)):
                failed.append(refused)
            elif tree.unpushed and push(tree):
                self.record(store, ledger, who, tree)
            owed = owed or tree.dirty or (tree.unpushed and not self.on_origin(tree))
            owed = owed or (doc and tree.own and not task.get("pr_url") and self.unrecorded(store, doc, who))
        if not owed:
            self.settle(store, who)
            return Decision()
        self.notify(store, who)
        return Decision.deny(" ".join([TEMPLATE, *failed]))

    def on_origin(self, tree):
        return count(tree.path, "HEAD", "--not", "--remotes=origin") == 0

    def unrecorded(self, store, doc, who):
        from scripts.gates.progress import Progress

        mark = Progress(store.redis, who.swarm).read(who.name)
        return mark.outcome == PUSHED and not spoke_since(
            doc.get("_meta", {}).get("events", []), who.name, mark.outcome_at
        )

    def record(self, store, ledger, who, tree):
        from scripts.gates.progress import Progress
        from scripts.swarm.store import SwarmError

        remote = git(tree.path, "remote", "get-url", "origin").stdout.strip()
        head = git(tree.path, "rev-parse", "HEAD").stdout.strip()
        Progress(store.redis, who.swarm).outcome(who.name, PUSHED, self.now())
        try:
            ledger.comment(who.swarm, who.task, record_text(remote, tree.branch, head), by=SENDER)
        except SwarmError:
            pass

    def notify(self, store, who):
        from scripts.inbox.store import CLOSED, InboxStore

        inbox = InboxStore(store.redis)
        address = inbox.names.resolve(who.name)
        if not any(item.text == TEMPLATE and item.state not in CLOSED for item in inbox.inbox(address)):
            inbox.send(SENDER, address, TEMPLATE)

    def settle(self, store, who):
        from scripts.inbox.store import CLOSED, InboxStore

        inbox = InboxStore(store.redis)
        for item in inbox.inbox(inbox.names.resolve(who.name)):
            if item.text == TEMPLATE and item.state not in CLOSED:
                inbox.close(item.id, SENDER, "done", SETTLED)

    def root(self):
        return self._root or default_root()

    def connect(self):
        if self._connect:
            return self._connect()
        from scripts.swarm.store import connect

        return connect()

    def ledger(self):
        if self._ledger:
            return self._ledger()
        from scripts.swarm.ledger_client import LedgerClient

        return LedgerClient()

    def now(self):
        if self._now:
            return self._now()
        import time

        return int(time.time() * 1000)
