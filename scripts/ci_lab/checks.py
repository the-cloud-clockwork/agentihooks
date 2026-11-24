"""Lab check runs posted through the GitHub App with a token that carries only checks write.

`Checks.post` creates the check run `lab/<gate>`, or `lab-shadow/<gate>` for a shadow grade, of one head and
grade run on its first transition and advances it in place after, forward only through queued, in_progress and
completed. `Checks.batch` folds one scheduler pass to the last transition of each check run. A rate limited
answer backs off with the limits of the CI budget's GitHub API wrapper.
"""

import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from itertools import accumulate
from typing import Any, Protocol

import requests

from scripts.ci_budget.rate_limit import LIMITED, MAX_WAIT, SECONDARY_WAIT, WAITS
from scripts.ci_lab import metrics
from scripts.ci_lab.units import Unit
from scripts.ci_lab.webhooks import CHECK_PREFIX
from scripts.github_app import GitHubAppAuth

__all__ = [
    "AUTHORITATIVE",
    "CONCLUSIONS",
    "PERMISSIONS",
    "SHADOW",
    "STATUSES",
    "TEXT_LIMIT",
    "Checks",
    "Output",
    "PostRefused",
    "Token",
    "Transition",
    "name",
    "render",
]

API = "https://api.github.com"
AUTHORITATIVE = CHECK_PREFIX
SHADOW = f"{CHECK_PREFIX}-shadow"
PERMISSIONS = {"checks": "write"}
STATUSES = ("queued", "in_progress", "completed")
CONCLUSIONS = frozenset({"action_required", "cancelled", "failure", "neutral", "skipped", "success", "timed_out"})
TEXT_LIMIT = 65535
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


def name(gate: str, shadow: bool) -> str:
    """Return the check run name of a gate: lab/<gate>, or lab-shadow/<gate> for a shadow grade."""
    return f"{SHADOW if shadow else AUTHORITATIVE}/{gate}"


def render(output: Output) -> dict[str, str]:
    """Return the check run output: the run id, grader revision and details link, the log tail of each failed unit
    and every unit key."""
    logs = _logs(output)
    text, listed = _listing(output, TEXT_LIMIT - len(logs))
    facts = [
        f"Run: {output.run}",
        f"Grader revision: {output.grader}",
        f"Details: {output.details}",
        f"Unit keys: {listed} of {len(output.units)} listed",
    ]
    return {"title": output.title, "summary": "\n".join([output.summary, "", *facts]), "text": logs + text}


def _logs(output: Output) -> str:
    """Return a block per unit log holding its last LOG_TAIL characters, within LOGS_LIMIT so the unit key listing
    keeps the rest of GitHub's limit; past it, the blocks that fit and a note that the later ones are left out."""
    blocks = [_block(unit, log[-LOG_TAIL:]) for unit, log in output.logs]
    note = f"Log tails of {len(blocks)} failed units exceed half of GitHub's limit; the later ones are left out.\n"
    return _fitting(blocks, LOGS_LIMIT, note)[0]


def _block(unit: Unit, tail: str) -> str:
    """Return a log tail fenced under its unit key, the fence one backtick longer than any backtick run in the tail."""
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall("`+", tail))])
    return f"Log tail of {unit.kind} `{unit.key}`:\n{fence}\n{tail}\n{fence}\n"


def _listing(output: Output, limit: int) -> tuple[str, int]:
    """Return a line per unit key and the count listed; past the limit, the lines that fit and a pointer."""
    lines = [f"- {unit.kind} `{unit.key}`\n" for unit in output.units]
    return _fitting(lines, limit, f"The full list of {len(lines)} unit keys is at {output.details}")


def _fitting(parts: list[str], limit: int, note: str) -> tuple[str, int]:
    """Return every part and their count when all fit in the limit; past it, the leading parts that fit beside the
    note, then the note, and their count."""
    if sum(map(len, parts)) <= limit:
        return "".join(parts), len(parts)
    fits = sum(1 for size in accumulate(map(len, parts)) if size + len(note) <= limit)
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
        self.auth: Token = auth if auth is not None else GitHubAppAuth(f"github.com/{repo}", permissions=PERMISSIONS)
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

    def _send(self, method: str, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Send one check runs request, waiting out rate limits within the CI budget's limits; return its JSON."""
        slept = 0.0
        for _ in range(WAITS):
            response = self._request(method, path, body)
            wait = _wait(response, slept, self.clock())
            if wait is None:
                return _json(response, method, path)
            self.sleep(wait)
            slept += wait
        return _json(self._request(method, path, body), method, path)

    def _request(self, method: str, path: str, body: Mapping[str, Any]) -> requests.Response:
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
