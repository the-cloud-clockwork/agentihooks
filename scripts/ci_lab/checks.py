"""Lab check runs posted with the workflow token inside GitHub Actions, else through the GitHub App with checks write.

`Checks.post` creates the check run `lab/<gate>`, or `lab-shadow/<gate>` for a shadow grade, of one head and
grade run on its first transition and advances it in place after, forward only through queued, in_progress and
completed. `Checks.batch` folds one scheduler pass to the last transition of each check run. A rate limited
answer backs off with the limits of the CI budget's GitHub API wrapper.

`Dispatches` stands in for `Checks` where the App lacks checks write: it sends each check run move once as a
`lab-check` repository dispatch with a token holding contents write. The Lab check workflow runs `main` on it,
which posts the move with `Checks.upsert`: the head's check run of that name and the head's external id is
advanced, else created, so every dispatch and rerun of one head and name keeps one check run.
"""

import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from itertools import accumulate
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import requests

from scripts.ci_budget.rate_limit import LIMITED, MAX_WAIT, SECONDARY_WAIT, WAITS
from scripts.ci_lab import metrics
from scripts.ci_lab.units import Unit
from scripts.ci_lab.webhooks import CHECK_PREFIX
from scripts.github_app import GitHubAppAuth

__all__ = [
    "AUTHORITATIVE",
    "CHECK_RUN_BUDGET",
    "CONCLUSIONS",
    "DISPATCHED",
    "DISPATCH_BUDGET",
    "DISPATCH_TEXT",
    "EVENT",
    "PERMISSIONS",
    "SHADOW",
    "STATUSES",
    "TEXT_LIMIT",
    "Bearer",
    "Budget",
    "Checks",
    "Dispatches",
    "Output",
    "PostRefused",
    "Token",
    "Transition",
    "dispatched",
    "external",
    "main",
    "name",
    "render",
    "token",
]

API = "https://api.github.com"
AUTHORITATIVE = CHECK_PREFIX
SHADOW = f"{CHECK_PREFIX}-shadow"
PERMISSIONS = {"checks": "write"}
EVENT = "lab-check"
DISPATCHED = 0
ACTIONS = "GITHUB_ACTIONS"
WORKFLOW_TOKEN = "GITHUB_TOKEN"
SHA = re.compile("[0-9a-f]{40}")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
LISTED = 100
NO_CONTENT = 204
STATUSES = ("queued", "in_progress", "completed")
CONCLUSIONS = frozenset({"action_required", "cancelled", "failure", "neutral", "skipped", "success", "timed_out"})
TEXT_LIMIT = 65535
DISPATCH_TEXT = TEXT_LIMIT // 2
LOG_TAIL = 4096
LOGS_LIMIT = TEXT_LIMIT // 2
TIMEOUT = 30
ACCEPTED = (200, 201)
LIMITED_STATUSES = (403, 429)
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

Key = tuple[str, str, str]


class PostRefused(RuntimeError):
    """GitHub refused a check run request with no rate limit wait left to retry it."""


class Token(Protocol):
    """A source of GitHub tokens, such as the App auth."""

    def get_token(self) -> str:
        """Return a token valid for the next request."""
        ...


@dataclass(frozen=True)
class Output:
    """What a check run shows: title, summary, the grade's units, run id, grader revision, details link and the log
    of each failed unit whose tail it shows."""

    title: str
    summary: str
    units: tuple[Unit, ...]
    run: str
    grader: str
    details: str
    logs: tuple[tuple[Unit, str], ...] = ()


@dataclass(frozen=True)
class Transition:
    """One check run move a scheduler pass asks for."""

    head: str
    gate: str
    status: str
    conclusion: str | None
    output: Output
    shadow: bool


@dataclass(frozen=True)
class Bearer:
    """A fixed token, such as the workflow token GitHub Actions hands a run."""

    value: str

    def get_token(self) -> str:
        """Return the fixed token."""
        return self.value


@dataclass(frozen=True)
class Budget:
    """The text a check run output may take: all of it, the share the failed units' log tails may take, and how a part
    of it is measured."""

    text: int
    logs: int
    size: Callable[[str], int]


def _encoded(text: str) -> int:
    """Return the characters text takes inside a JSON request body, escapes included, as requests sends it."""
    return len(json.dumps(text)) - 2


CHECK_RUN_BUDGET = Budget(TEXT_LIMIT, LOGS_LIMIT, len)
DISPATCH_BUDGET = Budget(DISPATCH_TEXT, DISPATCH_TEXT // 2, _encoded)


def token(repo: str, environ: Mapping[str, str]) -> Token:
    """Return the workflow token inside GitHub Actions, else the App token of the repository with checks write."""
    if environ.get(ACTIONS) == "true" and environ.get(WORKFLOW_TOKEN):
        return Bearer(environ[WORKFLOW_TOKEN])
    return GitHubAppAuth(f"github.com/{repo}", permissions=PERMISSIONS)


def external(head: str) -> str:
    """Return the external id every check run the Lab check workflow posts on a head carries."""
    return f"{CHECK_PREFIX}:{head}"


def name(gate: str, shadow: bool) -> str:
    """Return the check run name of a gate: lab/<gate>, or lab-shadow/<gate> for a shadow grade."""
    return f"{SHADOW if shadow else AUTHORITATIVE}/{gate}"


def render(output: Output, budget: Budget = CHECK_RUN_BUDGET) -> dict[str, str]:
    """Return the check run output: the run id, grader revision and details link, the log tail of each failed unit
    within the budget's log share and every unit key that fits beside them in its text, each part measured as the
    budget measures it; GitHub's check run limit in characters by default."""
    logs = _logs(output, budget)
    text, listed = _listing(output, budget.text - budget.size(logs), budget.size)
    facts = [
        f"Run: {output.run}",
        f"Grader revision: {output.grader}",
        f"Details: {output.details}",
        f"Unit keys: {listed} of {len(output.units)} listed",
    ]
    return {"title": output.title, "summary": "\n".join([output.summary, "", *facts]), "text": logs + text}


def _logs(output: Output, budget: Budget) -> str:
    """Return a block per unit log holding its last LOG_TAIL characters, within the budget's log share so the unit key
    listing keeps the rest of its text; past it, the blocks that fit and a note that the later ones are left out."""
    blocks = [_block(unit, log[-LOG_TAIL:]) for unit, log in output.logs]
    note = f"Log tails of {len(blocks)} failed units exceed half of GitHub's limit; the later ones are left out.\n"
    return _fitting(blocks, budget.logs, note, budget.size)[0]


def _block(unit: Unit, tail: str) -> str:
    """Return a log tail fenced under its unit key, the fence one backtick longer than any backtick run in the tail."""
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall("`+", tail))])
    return f"Log tail of {unit.kind} `{unit.key}`:\n{fence}\n{tail}\n{fence}\n"


def _listing(output: Output, limit: int, size: Callable[[str], int]) -> tuple[str, int]:
    """Return a line per unit key and the count listed; past the limit, the lines that fit and a pointer."""
    lines = [f"- {unit.kind} `{unit.key}`\n" for unit in output.units]
    return _fitting(lines, limit, f"The full list of {len(lines)} unit keys is at {output.details}", size)


def _fitting(parts: list[str], limit: int, note: str, size: Callable[[str], int]) -> tuple[str, int]:
    """Return every part and their count when all fit in the limit as size measures them; past it, the leading parts
    that fit beside the note, then the note, and their count."""
    if sum(map(size, parts)) <= limit:
        return "".join(parts), len(parts)
    fits = sum(1 for total in accumulate(map(size, parts)) if total + size(note) <= limit)
    return "".join(parts[:fits]) + note, fits


def _advance(before: str | None, status: str, conclusion: str | None) -> None:
    """Refuse an unknown status or conclusion, a move that is not forward, and a conclusion off a completed run."""
    if status not in STATUSES:
        raise ValueError(f"Unknown check run status: {status}")
    if before is not None and STATUSES.index(status) <= STATUSES.index(before):
        raise ValueError(f"A check run moves forward only, not from {before} to {status}")
    if (status == "completed") != (conclusion is not None):
        raise ValueError("A check run carries a conclusion exactly when it is completed")
    if conclusion is not None and conclusion not in CONCLUSIONS:
        raise ValueError(f"Unknown check run conclusion: {conclusion}")


def _wait(response: requests.Response, slept: float, now: float) -> float | None:
    """Return the seconds to wait before retrying a rate limited answer, or None when no wait applies."""
    left = MAX_WAIT - slept
    if left <= 0 or response.status_code not in LIMITED_STATUSES or not LIMITED.search(response.text):
        return None
    reset = response.headers.get("x-ratelimit-reset")
    if reset is None or "secondary" in response.text.lower():
        return min(left, SECONDARY_WAIT)
    return min(left, max(1.0, float(reset) - now + 1))


def _json(response: requests.Response, method: str, path: str) -> dict[str, Any]:
    """Return the JSON of an accepted answer, or raise PostRefused naming the request and its status."""
    if response.status_code not in ACCEPTED:
        raise PostRefused(f"{method} {path} answered {response.status_code}")
    data: dict[str, Any] = response.json()
    return data


def _retried(
    send: Callable[[], requests.Response], sleep: Callable[[float], None], clock: Callable[[], float]
) -> requests.Response:
    """Send a request, waiting out rate limited answers within the CI budget's limits; return the last answer."""
    slept = 0.0
    for _ in range(WAITS):
        response = send()
        wait = _wait(response, slept, clock())
        if wait is None:
            return response
        sleep(wait)
        slept += wait
    return send()


def dispatched(payload: Mapping[str, Any], stamp: str) -> tuple[str, str, dict[str, Any]]:
    """Return the head, check run name and check run body a lab-check dispatch payload carries, a completed one
    stamped as completed at the stamp; refuse a head that is not a full sha, a name outside the lab's check runs and a
    bad status or conclusion."""
    head, check = str(payload["sha"]), str(payload["name"])
    if not SHA.fullmatch(head):
        raise ValueError(f"A lab check dispatch names a full head sha, not {head!r}")
    if not check.startswith((f"{AUTHORITATIVE}/", f"{SHADOW}/")):
        raise ValueError(f"A lab check dispatch names a lab check run, not {check!r}")
    status, conclusion = payload["status"], payload.get("conclusion")
    _advance(None, status, conclusion)
    body: dict[str, Any] = {
        "status": status,
        "external_id": external(head),
        "details_url": payload["details_url"],
        "output": payload["output"],
    }
    if conclusion is not None:
        body["conclusion"] = conclusion
        body["completed_at"] = stamp
    return head, check, body


class Checks:
    """The lab's check runs on one repository, posted with a token that carries only checks write."""

    def __init__(
        self,
        repo: str,
        auth: Token | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        registry: metrics.Registry | None = None,
    ) -> None:
        self.repo = repo
        self.auth: Token = auth if auth is not None else token(repo, os.environ)
        self.sleep = sleep
        self.clock = clock
        self.registry = registry
        self.runs: dict[Key, tuple[int, str]] = {}

    def status(self, key: Key) -> str | None:
        """Return the last status posted for a check run, or None before its first post."""
        known = self.runs.get(key)
        return None if known is None else known[1]

    def seed(self, head: str, gate: str, run: str, run_id: int, status: str, shadow: bool) -> None:
        """Remember a check run posted before this poster existed, so its next post advances it in place."""
        self.runs[(head, name(gate, shadow), run)] = (run_id, status)

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Create the check run on its first transition, else advance it in place; return its id."""
        key = (head, name(gate, shadow), output.run)
        _advance(self.status(key), status, conclusion)
        body: dict[str, Any] = {
            "status": status,
            "external_id": output.run,
            "details_url": output.details,
            "output": render(output),
        }
        if conclusion is not None:
            body["conclusion"] = conclusion
        known = self.runs.get(key)
        if known is None:
            data = self._send("POST", "check-runs", {"name": key[1], "head_sha": head, **body})
        else:
            data = self._send("PATCH", f"check-runs/{known[0]}", body)
        run = int(data["id"])
        self.runs[key] = (run, status)
        return run

    def batch(self, transitions: Iterable[Transition]) -> list[int]:
        """Post one pass of transitions, each check run once at its last move; refuse a bad move before any request."""
        latest: dict[Key, Transition] = {}
        for move in transitions:
            key = (move.head, name(move.gate, move.shadow), move.output.run)
            _advance(latest[key].status if key in latest else self.status(key), move.status, move.conclusion)
            latest[key] = move
        return [self.post(m.head, m.gate, m.status, m.conclusion, m.output, m.shadow) for m in latest.values()]

    def upsert(self, head: str, check: str, body: Mapping[str, Any]) -> tuple[str, int]:
        """Advance the head's check run of this name and the body's external id, else create it; return which was done
        and the check run's id."""
        query = urlencode({"check_name": check, "filter": "all", "per_page": LISTED})
        listed = self._send("GET", f"commits/{head}/check-runs?{query}", None)["check_runs"]
        known = [run["id"] for run in listed if run.get("external_id") == body["external_id"]]
        if known:
            return "advanced", int(self._send("PATCH", f"check-runs/{known[0]}", body)["id"])
        return "created", int(self._send("POST", "check-runs", {"name": check, "head_sha": head, **body})["id"])

    def _send(self, method: str, path: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """Send one check runs request, waiting out rate limits within the CI budget's limits; return its JSON."""
        return _json(_retried(partial(self._request, method, path, body), self.sleep, self.clock), method, path)

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None) -> requests.Response:
        """Send one check runs request with a fresh token, setting the registry's GitHub calls left from its answer
        when the poster has a registry."""
        response = requests.request(
            method,
            f"{API}/repos/{self.repo}/{path}",
            headers={"Authorization": f"Bearer {self.auth.get_token()}", **HEADERS},
            json=body,
            timeout=TIMEOUT,
        )
        if self.registry is not None:
            metrics.observe_rate(self.registry, response.headers)
        return response


class Dispatches:
    """The lab's check runs on one repository handed to the Lab check workflow as `lab-check` repository dispatches,
    each move sent once with a token that carries only contents write."""

    def __init__(
        self,
        repo: str,
        auth: Token | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        registry: metrics.Registry | None = None,
    ) -> None:
        self.repo = repo
        self.auth: Token = auth if auth is not None else GitHubAppAuth(f"github.com/{repo}")
        self.sleep = sleep
        self.clock = clock
        self.registry = registry
        self.runs: dict[Key, str] = {}

    def status(self, key: Key) -> str | None:
        """Return the last status dispatched for a check run, or None before its first dispatch."""
        return self.runs.get(key)

    def seed(self, head: str, gate: str, run: str, run_id: int, status: str, shadow: bool) -> None:
        """Remember a check run dispatched before this poster existed, so it is not dispatched again; the id saved for
        it is DISPATCHED, since only the workflow learns the check run's own."""
        self.runs[(head, name(gate, shadow), run)] = status

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Dispatch one check run move to the Lab check workflow and return DISPATCHED; raise PostRefused when GitHub
        does not accept the dispatch."""
        key = (head, name(gate, shadow), output.run)
        _advance(self.status(key), status, conclusion)
        payload: dict[str, Any] = {
            "sha": head,
            "name": key[1],
            "status": status,
            "details_url": output.details,
            "output": render(output, DISPATCH_BUDGET),
        }
        if conclusion is not None:
            payload["conclusion"] = conclusion
        body = {"event_type": EVENT, "client_payload": payload}
        response = _retried(partial(self._request, body), self.sleep, self.clock)
        if response.status_code != NO_CONTENT:
            raise PostRefused(f"POST dispatches answered {response.status_code}")
        self.runs[key] = status
        return DISPATCHED

    def _request(self, body: Mapping[str, Any]) -> requests.Response:
        """Send one repository dispatch request with a fresh token, setting the registry's GitHub calls left from its
        answer when the poster has a registry."""
        response = requests.request(
            "POST",
            f"{API}/repos/{self.repo}/dispatches",
            headers={"Authorization": f"Bearer {self.auth.get_token()}", **HEADERS},
            json=body,
            timeout=TIMEOUT,
        )
        if self.registry is not None:
            metrics.observe_rate(self.registry, response.headers)
        return response


def main(environ: Mapping[str, str], now: Callable[[], float] = time.time) -> int:
    """Post the check run of the lab-check repository dispatch that GitHub Actions names in its event file, with the
    token `token` picks, print what was done and return 0."""
    event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text())
    head, check, body = dispatched(event["client_payload"], time.strftime(STAMP, time.gmtime(now())))
    repo = environ["GITHUB_REPOSITORY"]
    done, run = Checks(repo, token(repo, environ)).upsert(head, check, body)
    print(f"{done} {check} on {head} as check run {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.environ))
