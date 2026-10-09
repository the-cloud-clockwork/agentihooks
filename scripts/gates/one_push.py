"""The one push gate: a swarm worker opens its pull request once both reviews close and its work is on origin, and does
not push while that pull request's checks run unless one is already red, since a push cancels the running checks."""

import os
import re
from pathlib import Path, PurePosixPath

from scripts.gates.base import Decision
from scripts.gates.budget import Budget
from scripts.gates.identity import program_index, simple_commands
from scripts.gates.push_stop import GITHUB_RE, count, git
from scripts.gates.subagents import READERS, SubagentBudget
from scripts.swarm.naming import lane_of

WORKERS = frozenset({"eng", "ci"})
OPENS = (["pr", "create"], ["pr", "ready"])
DRAFTS = frozenset({"--draft", "-d", "--undo"})
NO_PUSH = frozenset({"--dry-run", "-n", "--delete", "-d"})
VALUED = frozenset({"-C", "-c"})
GH_VALUED = frozenset({"-R", "--repo"})
PULL_REPO = re.compile(r"github\.com/([^/]+/[^/]+)/pull/\d+")
MATCHED = re.compile(r"\bpush\b|\bpr\s+(?:create|ready)\b")


def _git_args(here, args):
    while args and args[0].startswith("-"):
        if args[0] == "-C" and len(args) > 1:
            here = here / Path(args[1]).expanduser()
        args = args[2:] if args[0] in VALUED else args[1:]
    return here, args


def _gh_args(args):
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in GH_VALUED else args[1:]
    return args


def actions(command, cwd):
    """Each pull request open and push the command runs, with the directory it runs in and its arguments."""
    here = Path(cwd or ".")
    for words in simple_commands(command):
        index = program_index(words)
        if index is None:
            continue
        program, rest = PurePosixPath(words[index]).name, words[index + 1 :]
        if program == "cd" and rest:
            here = here / Path(rest[0]).expanduser()
        elif program == "gh" and _gh_args(rest)[:2] in OPENS and not DRAFTS.intersection(rest):
            yield "open", here, rest
        elif program == "git":
            where, args = _git_args(here, rest)
            if args[:1] == ["push"] and not NO_PUSH.intersection(args):
                yield "push", where, args[1:]


def unsaved(path):
    """What of the checkout is not yet committed and on origin: '' when all of it is, or when it is no checkout."""
    status = git(path, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        return ""
    if status.stdout.strip():
        return "uncommitted changes"
    return "commits not on origin" if count(path, "HEAD", "--not", "--remotes=origin") else ""


def repo_of(path, args):
    """The repository a push writes: its remote argument as a URL or a remote name, origin when it names none."""
    remote = next((arg for arg in args if not arg.startswith("-")), "origin")
    url = remote if "/" in remote else git(path, "remote", "get-url", remote).stdout.strip()
    found = GITHUB_RE.search(url)
    return f"{found.group(1)}/{found.group(2)}".lower() if found else ""


def destinations(path, args):
    """The branches a push writes: each refspec's destination, the current branch for HEAD or no refspec."""
    current = git(path, "branch", "--show-current").stdout.strip()
    specs = [arg for arg in args if not arg.startswith("-")][1:]
    ends = [spec.lstrip("+").split(":")[-1].removeprefix("refs/heads/") for spec in specs] or [""]
    return {current if end in ("", "HEAD") else end for end in ends}


def open_refusal(slug, missing, held):
    owed = []
    if missing:
        agents = "sub-agent" if len(missing) == 1 else "sub-agents"
        owed.append(f"launch the {' and '.join(missing)} {agents} on the committed diff and close its findings")
    if held:
        owed.append(f"commit and push the {held}")
    return (
        f"open the pull request once review closes, so one push carries it: {'; '.join(owed)}. A draft pull request "
        f'for a block stays allowed: gh pr create --draft, then agentihooks swarm {slug} block "<why>"'
    )


def push_refusal(slug, url):
    return (
        f"checks still run on {url} and none is red: a push now cancels them. Wait with agentihooks swarm {slug} wait "
        f"--on checks {url}, then push once they resolve, or as soon as a check goes red"
    )


def queue_refusal(slug, url):
    return (
        f"{url} waits in the merge queue: a push now drops it. Dequeue first with agentihooks swarm {slug} merge "
        f"dequeue {url}, then push"
    )


class OnePush:
    name = "one-push"
    default_mode = "enforce"

    def __init__(self, ledger=None, github=None, target=None):
        self._ledger, self._github, self._target = ledger, github, target

    def matches(self, call):
        return call.tool == "Bash" and bool(MATCHED.search(call.command))

    def decide(self, call, who, state):
        if not (who.pinned and who.task) or lane_of(who.name) not in WORKERS:
            return Decision()
        for kind, where, args in actions(call.command, call.cwd):
            reason = self.opening(who, state, where) if kind == "open" else self.pushing(who, where, args)
            if reason:
                return Decision.deny(reason)
        return Decision()

    def opening(self, who, state, where):
        budget = Budget(state.slug, SubagentBudget.name, state.home)
        missing = [name for name in READERS if not budget.spent(who.task, name)] if self.target() == "claude" else []
        held = unsaved(where)
        return open_refusal(who.swarm, missing, held) if missing or held else ""

    def pushing(self, who, where, args):
        task = next((t for t in self.ledger().tasks(who.swarm) if t.get("id") == who.task), None) or {}
        found = PULL_REPO.search(task.get("pr_url") or "")
        if not found:
            return ""
        repo = repo_of(where, args)
        if repo and repo != found.group(1).lower():
            return ""
        if task.get("branch") and task["branch"] not in destinations(where, args):
            return ""
        pull = self.github()(task["pr_url"])
        if pull is None or pull.state != "OPEN" or pull.red:
            return ""
        if pull.queued:
            return queue_refusal(who.swarm, task["pr_url"])
        return push_refusal(who.swarm, task["pr_url"]) if pull.running else ""

    def target(self):
        return self._target or os.environ.get("AGENTIHOOKS_TARGET") or "claude"

    def ledger(self):
        if self._ledger:
            return self._ledger()
        from scripts.swarm.ledger_client import LedgerClient

        return LedgerClient()

    def github(self):
        if self._github:
            return self._github
        from scripts.swarm.ledger_events import view

        return view
