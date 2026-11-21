"""The CI lab relay: a green grade of a `refs/grade/<task>` head pushed with a branch reaches GitHub.

`Relay.tick` reads the lab remote's head notices from its own consumer and keeps each trusted actor's pushed
`refs/grade/*` head that names a `branch=<name>` push option outside the protected branches pending in Redis, per
head and branch, until a grade of that head closes. A green grade pushes the sha, fetched from the lab remote when
GitHub lacks it, to `origin refs/heads/<branch>` with an App token holding contents and workflows write, never forced,
posts the `lab/<gate>` check run of every gate and the required check, completed, with the grade's run and verdicts,
once across restarts by keeping each check run's id on the grade's state, and opens the branch's draft pull request
into dev with a token holding pull requests write, or names the open one the push updated. A red grade relays nothing
and names its failed units. A head whose grades were all superseded is dropped, and a relay GitHub refuses is named
and dropped. A relay whose App token GitHub refuses is named held, stays pending and holds every relay of its
repository for the scheduler's post hold.
"""

import base64
import json
import os
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import requests

from scripts.ci_lab import contract, evidence, worktrees
from scripts.ci_lab.checks import API, HEADERS, Checks, Key, Output, PostRefused, Token, name
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.outcomes import PASSED
from scripts.ci_lab.remote.post_receive import BRANCH, GRADE_REFS, LAB_URL
from scripts.ci_lab.scheduler import BATCH, FETCH_MS, MIRROR_STORE, POST_HOLD_MS, REQUIRED, Scheduler, State
from scripts.ci_lab.store import PREFIX
from scripts.ci_lab.units import Unit
from scripts.ci_lab.webhooks import HEAD_SUBJECT, NOTICE
from scripts.ci_lab.worktrees import Mirror
from scripts.events.bus import Consumer
from scripts.github_app import GitAuthRefused, GitHubAppAuth

__all__ = [
    "DURABLE",
    "PENDING",
    "PROTECTED",
    "PULLS",
    "PUSH",
    "SUBJECT",
    "BusNotices",
    "GitPusher",
    "Pending",
    "Ports",
    "PullRequests",
    "Relay",
    "RelayRefused",
    "build",
    "closed",
    "failed",
    "output",
    "pending",
    "relay",
]

PENDING = f"{PREFIX}relay"
DURABLE = "ci-lab-relay"
SUBJECT = f"{HEAD_SUBJECT}.{NOTICE}"
PULLS = {"pull_requests": "write"}
PUSH = {"contents": "write", "workflows": "write"}
GITHUB = "https://github.com/"
DEV = "dev"
PROTECTED = frozenset({DEV, "main", "master", "v1"})
SUCCESS = "success"
TIMEOUT = 30
ACCEPTED = (200, 201)


class RelayRefused(RuntimeError):
    """GitHub refused a relay's push or pull request call."""


@dataclass(frozen=True)
class Pending:
    """A pushed grade ref head waiting for its grade: its repository, sha, grade ref and the branch it relays to."""

    repo: str
    head: str
    ref: str
    branch: str


class Pusher(Protocol):
    """What puts a graded sha on a GitHub branch."""

    def push(self, sha: str, branch: str) -> None:
        """Point the branch at the sha by a fast forward only."""
        ...


class Pulls(Protocol):
    """What opens or finds a branch's pull request."""

    def open(self, branch: str, title: str, body: str) -> str:
        """Open the branch's draft pull request, or find its open one; return which and its url."""
        ...


class CheckRuns(Protocol):
    """Where relayed check runs go: `Checks`, which remembers the last status of each run it posted or was seeded
    with."""

    def seed(self, head: str, gate: str, run: str, run_id: int, status: str, shadow: bool) -> None:
        """Remember a check run posted before this poster existed, so it is not created again."""
        ...

    def status(self, key: Key) -> str | None:
        """Return the last status posted for a check run, or None before its first post."""
        ...

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Create or advance one check run and return its id."""
        ...


class Notices(Protocol):
    """Where pending relays come from."""

    def pending(self) -> list[Pending]:
        """Return the relays the notices since the last call name."""
        ...


@dataclass(frozen=True)
class Ports:
    """The relay's outside world: a pusher per repository URL, check runs and pull requests per owner/name, where a
    grade's state is saved, and the wall clock in seconds."""

    pusher: Callable[[str], Pusher]
    checks: Callable[[str], CheckRuns]
    pulls: Callable[[str], Pulls]
    save: Callable[[State], None]
    clock: Callable[[], float] = time.time


def _slug(repo: str) -> str:
    """Return owner/name of a repository URL."""
    return urlsplit(repo).path.removeprefix("/")


def pending(data: Mapping[str, Any]) -> Pending | None:
    """Return the relay a head notice's data names: a pushed refs/grade/* head with a branch outside the protected
    branches, else None."""
    ref, branch = data.get("ref"), data.get(BRANCH)
    if not isinstance(ref, str) or not ref.startswith(GRADE_REFS) or not branch or branch in PROTECTED:
        return None
    return Pending(str(data["repo"]), str(data["head"]), ref, str(branch))


def failed(state: State) -> list[str]:
    """Return the refs of a grade's units whose outcome is not passed, in grade order."""
    return [ref for ref, slot in state.units.items() if slot.outcome != PASSED]


def output(state: State, check: str, lab: str) -> Output:
    """Return a relayed check run's output: the check's units, none for a skipped authoritative gate, the grade run,
    the grader revision and the grade page."""
    refs = list(state.units) if check == REQUIRED else state.gates.get(check, [])
    units = tuple(Unit(slot.kind, slot.key, tuple(slot.members)) for slot in (state.units[ref] for ref in refs))
    summary = f"{len(units)} units of {check} on {state.head} against {state.base}, relayed from the lab grade"
    details = f"{lab}/grades/{state.head}"
    return Output(f"{CLASSES[state.tier]} grade relayed", summary, units, state.run, state.grader, details)


def relay(state: State, branch: str, ports: Ports, lab: str) -> list[str]:
    """Relay a closed grade: a green one pushes the sha to the branch, posts every check run completed that is not
    already, and opens the draft pull request, in that order; a red one relays nothing and names its failed units. Each
    check run the relay posts is saved on the grade's state under its check run name as GitHub answers, and seeds the
    poster before the next relay of the grade, so a relay retried after a restart creates no second check run; the ids
    the scheduler stores under gate names for its own posts never seed it."""
    if state.conclusion != SUCCESS:
        units = ", ".join(failed(state)) or "none"
        return [f"held {state.head} for {branch}: grade {state.run} is {state.conclusion}, failed units {units}"]
    slug = _slug(state.repo)
    ports.pusher(state.repo).push(state.head, branch)
    actions = [f"pushed {state.head} to {branch}"]
    poster = ports.checks(slug)
    for check, (_, conclusion) in state.posted.items():
        relayed = name(check, False)
        if relayed in state.runs:
            poster.seed(state.head, check, state.run, state.runs[relayed], "completed", False)
        if poster.status((state.head, relayed, state.run)) is None:
            state.runs[relayed] = poster.post(
                state.head, check, "completed", conclusion, output(state, check, lab), False
            )
            ports.save(state)
            actions.append(f"posted {relayed} {conclusion} on {state.head}")
    body = f"Relayed by the CI lab from grade {state.run}: {lab}/grades/{state.head}"
    actions.append(ports.pulls(slug).open(branch, branch, body))
    return actions


class Relay:
    """The relay's pending heads in Redis, read from its notices and relayed once a grade of each closes."""

    def __init__(
        self, client: Any, notices: Notices, grades: Callable[[str], list[State]], ports: Ports, lab: str
    ) -> None:
        self.client = client
        self.notices = notices
        self.grades = grades
        self.ports = ports
        self.lab = lab
        self.held: defaultdict[str, int] = defaultdict(int)

    def tick(self) -> list[str]:
        """Record the new notices' relays per head and branch, then relay each pending one a closed grade answers and
        forget it once relayed; a relay whose git or HTTP call fails is named and stays pending for the next tick, and a
        relay whose App token GitHub refuses is named held, stays pending and holds every relay of its repository for
        POST_HOLD_MS while other repositories still relay."""
        actions: list[str] = []
        now_ms = int(self.ports.clock() * 1000)
        for item in self.notices.pending():
            self.client.hset(PENDING, f"{item.head} {item.branch}", json.dumps(asdict(item)))
            actions.append(f"pending {item.head} for {item.branch}")
        for key, raw in sorted(self.client.hgetall(PENDING).items()):
            item = Pending(**json.loads(raw))
            if now_ms < self.held[item.repo]:
                continue
            states = self.grades(item.head)
            if not states:
                continue
            try:
                actions.extend(self._relay(item, states))
            except (OSError, subprocess.SubprocessError) as error:
                actions.append(f"retry {item.head} for {item.branch}: {error}")
                continue
            except GitAuthRefused as error:
                self.held[item.repo] = now_ms + POST_HOLD_MS
                actions.append(f"held {item.head} for {item.branch} for a later pass: {error}")
                continue
            self.client.hdel(PENDING, key)
        return actions

    def _relay(self, item: Pending, states: list[State]) -> list[str]:
        """Relay a head's green grade, else hold its red one; drop a head whose grades were all superseded."""
        live = [state for state in states if not state.superseded]
        if not live:
            return [f"dropped {item.head} for {item.branch}: every grade superseded"]
        state = next((state for state in live if state.conclusion == SUCCESS), live[0])
        try:
            return relay(state, item.branch, self.ports, self.lab)
        except (RelayRefused, PostRefused) as error:
            return [f"refused {item.head} for {item.branch}: {error}"]


class BusNotices:
    """Relays read from the bus consumer of lab head notices, kept only for a trusted actor's push, each envelope
    acknowledged once read."""

    def __init__(self, consumer: Consumer, trusted: Collection[str]) -> None:
        self.consumer = consumer
        self.trusted = trusted

    def pending(self) -> list[Pending]:
        """Return the relays of the next batch of notices whose actor is trusted."""
        found: list[Pending] = []
        for delivery in self.consumer.fetch(BATCH, FETCH_MS):
            data = cast(Mapping[str, Any], delivery.event.data)
            item = pending(data)
            if item is not None and data.get("actor") in self.trusted:
                found.append(item)
            delivery.ack()
        return found


class GitPusher:
    """Pushes a graded sha from the lab's git mirror to a GitHub branch with an App token holding contents and workflows
    write, so a branch that changes workflow files is accepted."""

    def __init__(self, mirror: Mirror, slug: str, auth: Token | None = None) -> None:
        self.mirror = mirror
        self.slug = slug
        self.auth: Token = auth if auth is not None else GitHubAppAuth(f"github.com/{slug}", permissions=PUSH)

    def push(self, sha: str, branch: str) -> None:
        """Make the sha present in the mirror's store and push it to the branch, never forced, with the token passed
        through git's environment config; raise RelayRefused with git's reason on a refusal."""
        self.mirror.fetch(sha)
        secret = base64.b64encode(f"x-access-token:{self.auth.get_token()}".encode()).decode()
        environ = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{GITHUB}.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {secret}",
            "GIT_TERMINAL_PROMPT": "0",
        }
        argv = ["git", "-C", str(self.mirror.store), "push", f"{GITHUB}{self.slug}.git", f"{sha}:refs/heads/{branch}"]
        done = subprocess.run(argv, env=environ, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RelayRefused(f"git push of {sha} to {branch} answered {done.returncode}: {done.stderr.strip()}")


class PullRequests:
    """A repository's pull requests through the App with a token holding pull requests write."""

    def __init__(self, slug: str, auth: Token | None = None) -> None:
        self.slug = slug
        self.auth: Token = auth if auth is not None else GitHubAppAuth(f"github.com/{slug}", permissions=PULLS)

    def open(self, branch: str, title: str, body: str) -> str:
        """Name the branch's open pull request the push updated, else open its draft into dev; return which and its
        url."""
        owner = self.slug.partition("/")[0]
        found = self._send("GET", {"head": f"{owner}:{branch}", "state": "open"}, None)
        if found:
            return f"updated {found[0]['html_url']}"
        made = self._send("POST", None, {"title": title, "head": branch, "base": DEV, "body": body, "draft": True})
        return f"opened {made['html_url']}"

    def _send(self, method: str, params: Mapping[str, str] | None, body: Mapping[str, Any] | None) -> Any:
        """Send one pulls request with a fresh token and return its JSON; raise RelayRefused naming a refused one."""
        response = requests.request(
            method,
            f"{API}/repos/{self.slug}/pulls",
            headers={"Authorization": f"Bearer {self.auth.get_token()}", **HEADERS},
            params=params,
            json=body,
            timeout=TIMEOUT,
        )
        if response.status_code not in ACCEPTED:
            raise RelayRefused(f"{method} pulls answered {response.status_code}")
        return response.json()


def closed(scheduler: Scheduler, head: str) -> list[State]:
    """Return the closed grades of a head: the runs its evidence markers name that still have a state."""
    prefix = evidence.head_prefix(head)
    runs = (key.removeprefix(prefix) for key in scheduler.objects.keys(prefix))
    return [state for run in runs if (state := scheduler.grade(run)) is not None]


def build(environ: Mapping[str, str], scheduler: Scheduler, consumer: Consumer) -> Relay:
    """Return the relay over the scheduler's Redis and closed grades, pushing from the lab's git mirror stores, which
    fall back to the lab remote the environment names."""
    node, stores = Path(environ[contract.GIT_MIRROR]), Path(environ[MIRROR_STORE])
    stores.mkdir(parents=True, exist_ok=True)
    remote = worktrees.lab(environ)

    def pusher(repo: str) -> Pusher:
        slug = _slug(repo)
        return GitPusher(Mirror(node, stores / slug.replace("/", "--"), repo, remote), slug)

    posters: dict[str, CheckRuns] = {}

    def checks(slug: str) -> CheckRuns:
        if slug not in posters:
            posters[slug] = Checks(slug, registry=scheduler.registry)
        return posters[slug]

    ports = Ports(pusher, checks, PullRequests, scheduler.save)
    lab = environ[LAB_URL].rstrip("/")
    notices = BusNotices(consumer, scheduler.settings.trusted)
    return Relay(scheduler.client, notices, partial(closed, scheduler), ports, lab)
