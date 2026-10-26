"""The Stop gate that keeps a swarm worker's work on origin: it pushes the task branch itself, then refuses the stop over
uncommitted changes, a push origin refused, or pushed work with no pull request and no ledger line since the push,
unless a checked wait on another task not yet done, or on the merge of a pull request still open, holds it."""

import os
import re
import signal
import subprocess
import sys
import time
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
PROGRESS = (
    "Pushed work has no recorded progress since the push. "
    'Run agentihooks swarm {swarm} progress --doing "Describe current work" --ends-when "Describe completion". '
    "A checks wait does not waive recording progress."
)
SENDER = "swarm"
SETTLED = "a later stop passed with the work committed, on origin and recorded"
PUSHED = "pushed"
GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
GIT_TIMEOUT_S = 60
# Measured: a GitHub push takes up to 2.3 s, the ledger write after it up to 0.52 s and the hook's startup up to 0.23 s.
RESERVE_S = 3.5
FLOOR_S = 1.0
PREPUSH = Path("scripts") / "ci_prepush" / "__init__.py"
GATE_FAILED = (
    "The pre push gate failed in {path}, so the stop hook did not push it. "
    "Run python -m scripts.ci_prepush there, fix what fails and commit."
)
GATE_SLOW = (
    "The pre push gate did not finish in {seconds:g} s in {path}, so the stop hook did not push it. "
    "Run python -m scripts.ci_prepush there and commit."
)
GATE_LATE = (
    "The stop hook had too little time left to run the pre push gate in {path}, so it did not push it. "
    "Run python -m scripts.ci_prepush there and commit."
)
PUSH_LATE = "The stop hook had too little time left to push {path}, so it did not push it. Push it yourself."


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


def gate_refusal(tree, budget):
    """Why the pre push gate keeps HEAD off origin, or None: a repo without one passes, a stamped HEAD is not rerun."""
    if not (tree.path / PREPUSH).is_file():
        return None
    from scripts.ci_prepush import passed

    if passed(tree.path):
        return None
    seconds = budget()
    if seconds < FLOOR_S:
        return GATE_LATE.format(path=tree.path)
    gate = subprocess.Popen(
        [sys.executable, "-m", "scripts.ci_prepush"],
        cwd=tree.path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = gate.wait(seconds)
    except subprocess.TimeoutExpired:
        os.killpg(gate.pid, signal.SIGKILL)
        gate.wait()
        return GATE_SLOW.format(path=tree.path, seconds=round(seconds, 1))
    return None if code == 0 else GATE_FAILED.format(path=tree.path)


def push(tree, branch, seconds):
    ref = f"refs/heads/{branch}"
    lease = [] if branch == tree.branch else [f"--force-with-lease={ref}"]
    process = subprocess.Popen(
        ["git", "-C", str(tree.path), "push", *lease, "--set-upstream", "origin", f"refs/heads/{tree.branch}:{ref}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return process.wait(min(GIT_TIMEOUT_S, seconds)) == 0
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return False


def target(tree, task):
    """The retained pull request branch HEAD continues, as swarm publish pushes it, else the worktree's own branch."""
    from scripts.swarm.continuation import retained

    branch = retained(task)
    if not branch:
        return tree.branch
    continues = git(tree.path, "merge-base", "--is-ancestor", f"refs/remotes/origin/{branch}", "HEAD")
    return branch if continues.returncode == 0 else tree.branch


def record_text(remote, branch, head):
    found = GITHUB_RE.search(remote)
    if not found:
        return f"The stop hook pushed the task branch {branch} to origin"
    web = f"https://github.com/{found.group(1)}/{found.group(2)}"
    return f"The stop hook pushed the task branch {web}/tree/{branch} with its head at {web}/commit/{head}"


def own_pull(tree, task):
    """Whether the task's pull request sits in the worktree's own repository; any counts for an origin off GitHub."""
    url = (task.get("pr_url") or "").lower()
    found = GITHUB_RE.search(git(tree.path, "remote", "get-url", "origin").stdout.strip())
    return bool(url) and (not found or f"github.com/{found[1]}/{found[2]}/pull/".lower() in url)


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
    return item.sender == SENDER and (
        item.text == TEMPLATE
        or item.text.startswith(
            (
                "Pushed work has no recorded progress since the push.",
                TEMPLATE + " Pushed work has no recorded progress since the push.",
            )
        )
    )


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

    def __init__(self, connect=None, ledger=None, root=None, now=None, clock=None, github=None):
        self._connect, self._ledger, self._root, self._now, self._clock = connect, ledger, root, now, clock
        self._github = github
        # The hook process builds its gates right after its imports; the reserve covers the startup before this.
        self.started = self.clock()

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
        store, owed, progress, failed, found = self.connect(), False, False, [], trees(self.root(), who.name)
        for tree in found:
            if tree.unpushed and (refused := self.held(tree)):
                failed.append(refused)
            elif tree.unpushed and push(tree, branch := target(tree, task), self.left() + RESERVE_S - FLOOR_S):
                self.record(store, ledger, who, tree, branch)
            owed = owed or tree.dirty or (tree.unpushed and not self.on_origin(tree))
            progress = progress or (
                doc and tree.own and not own_pull(tree, task) and self.unrecorded(store, doc, who, found)
            )
        if not (owed or progress):
            self.settle(store, who)
            return Decision()
        if self.waiting(store, who, doc, task, any(tree.dirty for tree in found)):
            return Decision()
        reason = " ".join(
            filter(None, [TEMPLATE if owed else "", PROGRESS.format(swarm=who.swarm) if progress else ""])
        )
        self.notify(store, who, reason)
        return Decision.deny(" ".join([reason, *failed]))

    def held(self, tree):
        """Why HEAD stays off origin: its pre push gate refused, or less than the reserve is left for the push."""
        return gate_refusal(tree, self.left) or (PUSH_LATE.format(path=tree.path) if self.left() < 0 else None)

    def on_origin(self, tree):
        return count(tree.path, "HEAD", "--not", "--remotes=origin") == 0

    def waiting(self, store, who, doc, task, dirty):
        """Whether a live checked wait holds the stop, so a refused push never deadlocks: one on another unfinished
        task, or one on the merge of a pull request still open, which a push would drop from the queue, while every
        change is committed."""
        from scripts.swarm import idle, waits

        held = idle.wait(store.redis, who.swarm, who.name)
        on = (held or {}).get("on") or {}
        if not (held and held["until"] > self.now()):
            return False
        if on.get("kind") == "merge" and not dirty:
            pull = self.github()(on["target"])
            return pull is not None and pull.state == "OPEN"
        if not (doc and on.get("kind") == "task"):
            return False
        return waits.dependency(on, task, {row.get("id"): row for row in doc["tasks"]}, None)

    def unrecorded(self, store, doc, who, found):
        from scripts.gates.progress import Progress

        mark = Progress(store.redis, who.swarm).read(who.name)
        return (
            mark.outcome == PUSHED
            and not spoke_since(doc.get("_meta", {}).get("events", []), who.name, mark.outcome_at)
            and not any(self._preflight(store, who, tree) for tree in found)
        )

    def _preflight(self, store, who, tree):
        from scripts.swarm import idle, mutation_wait

        held = idle.wait(store.redis, who.swarm, who.name)
        if not held or held["until"] <= self.now():
            return False
        on = held.get("on") or {}
        if on.get("kind") != "mutation" or on.get("task") != who.task:
            return False
        run = mutation_wait.RUN_URL.fullmatch(on["target"])
        remote = GITHUB_RE.search(git(tree.path, "remote", "get-url", "origin").stdout.strip())
        if not run or not remote:
            return False
        return (
            run[1].lower() == f"{remote[1]}/{remote[2]}".lower()
            and on.get("head") == git(tree.path, "rev-parse", "HEAD").stdout.strip()
        )

    def record(self, store, ledger, who, tree, branch):
        from scripts.gates.progress import Progress
        from scripts.swarm.store import SwarmError

        remote = git(tree.path, "remote", "get-url", "origin").stdout.strip()
        head = git(tree.path, "rev-parse", "HEAD").stdout.strip()
        Progress(store.redis, who.swarm).outcome(who.name, PUSHED, self.now())
        try:
            ledger.comment(who.swarm, who.task, record_text(remote, branch, head), by=SENDER)
        except SwarmError:
            pass

    def notify(self, store, who, text):
        from scripts.inbox.store import CLOSED, InboxStore

        inbox = InboxStore(store.redis)
        address = inbox.names.resolve(who.name)
        if not any(item.text == text and item.state not in CLOSED for item in inbox.inbox(address)):
            inbox.send(SENDER, address, text)

    def settle(self, store, who):
        from scripts.inbox.store import CLOSED, InboxStore

        inbox = InboxStore(store.redis)
        for item in inbox.inbox(inbox.names.resolve(who.name)):
            if is_notice(item) and item.state not in CLOSED:
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

    def github(self):
        from scripts.swarm.ledger_events import view

        return self._github or view

    def now(self):
        if self._now:
            return self._now()
        return int(time.time() * 1000)

    def clock(self):
        return self._clock() if self._clock else time.monotonic()

    def left(self):
        """Seconds a pre push gate may run: the Stop condition's time left after the reserve."""
        return self.started + CONDITIONS_TIMEOUT_SEC - self.clock() - RESERVE_S
