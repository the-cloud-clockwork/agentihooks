"""The culprit of a red merge group: each entry graded alone against dev from the lab's verdict cache.

`name` reads each entry's alone verdict in queue order: the first entry red alone is the culprit, and a group whose
entries are all green alone is red only together, an interaction that names every entry. `body` renders the comment
the lab posts on each named pull request, `Culprits` posts it through the GitHub App and dequeues the entry the
finding removes so GitHub re-forms the queue without it, and `observation` is the hub pulls observation whose
`merge.dequeued` event carries `culprit`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from scripts.ci_lab.checks import HEADERS, TIMEOUT, PostRefused, Token
from scripts.ci_lab.store import Verdict
from scripts.ci_lab.units import Unit
from scripts.github_app import GitHubAppAuth
from scripts.hub.pulls import Observation

__all__ = [
    "CULPRIT",
    "DEQUEUE",
    "INTERACTION",
    "NODE",
    "PERMISSIONS",
    "Culprits",
    "Entry",
    "Finding",
    "Verdicts",
    "alone",
    "body",
    "name",
    "observation",
]

API = "https://api.github.com"
CULPRIT = "culprit"
INTERACTION = "interaction"
PERMISSIONS = {"contents": "write", "pull_requests": "write"}
CREATED = 201
ANSWERED = 200
NODE = "query($url: URI!) { resource(url: $url) { ... on PullRequest { id } } }"
DEQUEUE = "mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } } }"


class Verdicts(Protocol):
    """The verdict cache read a culprit needs, such as the lab store."""

    def get(self, kind: str, key: str) -> Verdict | None:
        """Return the cached verdict of a unit, or None when it misses."""
        ...


@dataclass(frozen=True)
class Entry:
    """One merge group entry in queue order: its pull request url and the units it runs graded alone against dev."""

    url: str
    units: tuple[Unit, ...]


@dataclass(frozen=True)
class Finding:
    """What a red group names: its culprit red alone, or the entries of an interaction red only together."""

    kind: str
    group: str
    urls: tuple[str, ...]

    @property
    def removed(self) -> str:
        """Return the entry that leaves the queue: the culprit, or the last entry of an interaction."""
        return self.urls[-1]


def alone(verdicts: Verdicts, entry: Entry) -> bool | None:
    """Return whether an entry is green alone: False on any red unit, None while a unit misses the cache."""
    cached = [verdicts.get(unit.kind, unit.key) for unit in entry.units]
    if any(verdict is not None and not verdict.passed for verdict in cached):
        return False
    return None if None in cached else True


def name(verdicts: Verdicts, group: str, entries: Sequence[Entry]) -> Finding | None:
    """Return the first entry red alone as the culprit, else an interaction of every entry; None while undecided."""
    raise NotImplementedError


def body(finding: Finding, details: str) -> str:
    """Return the comment the lab posts on each pull request a finding names."""
    raise NotImplementedError


def observation(finding: Finding, comments: Sequence[str]) -> Observation:
    """Return the hub observation that the removed entry left the queue, carrying the culprit its event names."""
    raise NotImplementedError


class Culprits:
    """Comments on and dequeues the pull requests a red group names, through the App on one repository."""

    def __init__(self, repo: str, auth: Token | None = None) -> None:
        self.repo = repo
        self.auth: Token = auth if auth is not None else GitHubAppAuth(f"github.com/{repo}", permissions=PERMISSIONS)

    def comment(self, url: str, text: str) -> str:
        """Post a comment on the pull request at url; return the comment's html url."""
        raise NotImplementedError

    def dequeue(self, url: str) -> None:
        """Remove the pull request at url from its merge queue, so GitHub re-forms the groups behind it."""
        raise NotImplementedError

    def _graphql(self, query: str, variables: Mapping[str, str]) -> dict[str, Any]:
        """Send one GraphQL request; return its data, or raise PostRefused naming its errors."""
        answer = self._send("graphql", {"query": query, "variables": dict(variables)}, ANSWERED)
        errors = answer.get("errors")
        if errors:
            raise PostRefused(f"POST graphql answered errors: {'; '.join(str(e['message']) for e in errors)}")
        data: dict[str, Any] = answer["data"]
        return data

    def _send(self, path: str, document: Mapping[str, Any], accepted: int) -> dict[str, Any]:
        """Send one POST with a fresh token; return its JSON, or raise PostRefused naming the request and status."""
        response = requests.request(
            "POST",
            f"{API}/{path}",
            headers={"Authorization": f"Bearer {self.auth.get_token()}", **HEADERS},
            json=dict(document),
            timeout=TIMEOUT,
        )
        if response.status_code != accepted:
            raise PostRefused(f"POST {path} answered {response.status_code}")
        data: dict[str, Any] = response.json()
        return data
