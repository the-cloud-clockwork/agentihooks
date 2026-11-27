"""The test merge GitHub builds for a pull request, read from its API and fetched by sha into the scheduler's
mirror.

`resolve` reads one pull request, takes its `merge_commit_sha` and the git parents of that commit, and returns the
merge and its first parent only when the merge's second parent is the head; GitHub answering with no merge yet
raises nothing, a refused or unanswered request raises `MergeUnanswered`, and `Background` runs those resolutions
off the scheduler pass on a thread pool, raising its subclass `MergePending` while one still runs. `Resolver` keeps
one GitHub reader per repository the lab grades, reading the repository's mirror through the port the scheduler
gives it.
"""

import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol

import requests

from scripts.ci_lab.checks import Token
from scripts.ci_lab.worktrees import Mirror
from scripts.github_app import GitAuthRefused, GitHubAppAuth

__all__ = [
    "ABSENT_S",
    "FRESH_S",
    "PERMISSIONS",
    "WORKERS",
    "Actions",
    "Background",
    "MergePending",
    "MergeRefused",
    "MergeUnanswered",
    "PullRequests",
    "Resolver",
    "resolve",
]

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
PERMISSIONS = {"pull_requests": "read"}
TIMEOUT = 30
OK = 200
SERVER_ERROR = 500
WORKERS = 4
FRESH_S = 30.0
ABSENT_S = 10.0


class MergeRefused(ValueError):
    """GitHub refused or never answered a pull request read."""


class MergeUnanswered(MergeRefused):
    """GitHub never answered a pull request read or answered it with a server error, so a later read may succeed."""


class MergePending(MergeUnanswered):
    """A merge resolution still runs off the scheduler pass, so a later call answers it."""


class PullRequests(Protocol):
    """One repository's GitHub pull requests API."""

    def json(self, path: str) -> dict[str, Any]:
        """Return the JSON GitHub answers to a GET of a path under the repository."""
        ...


def _accepted(response: requests.Response, path: str) -> requests.Response:
    """Return an answer GitHub accepted, or raise MergeRefused naming the request and its status, MergeUnanswered for
    a server error."""
    if response.status_code != OK:
        refused = MergeUnanswered if response.status_code >= SERVER_ERROR else MergeRefused
        raise refused(f"GET {path} answered {response.status_code}")
    return response


class Actions:
    """One repository's pull requests API, read with a token the lab's GitHub App scopes to pull requests read."""

    def __init__(self, repo: str, auth: Token | None = None) -> None:
        self.repo = repo
        self.auth: Token = auth if auth is not None else GitHubAppAuth(f"github.com/{repo}", permissions=PERMISSIONS)

    def json(self, path: str) -> dict[str, Any]:
        """Return the JSON of an accepted answer, or raise MergeRefused."""
        data: dict[str, Any] = _accepted(self._get(path), path).json()
        return data

    def _get(self, path: str) -> requests.Response:
        """Send one GET under the repository with a fresh token. A request that fails before GitHub answers it, or a
        token mint without an answer included, raises MergeUnanswered naming it."""
        try:
            return requests.get(
                f"{API}/repos/{self.repo}/{path}",
                headers={"Authorization": f"Bearer {self.auth.get_token()}", **HEADERS},
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            raise MergeUnanswered(f"GET {path} failed: {error}") from error
        except GitAuthRefused as error:
            refused = (
                MergeUnanswered if error.status_code is not None and error.status_code >= SERVER_ERROR else MergeRefused
            )
            raise refused(f"GET {path} token mint failed: {error}") from error


def _parents(mirror: Mirror, sha: str) -> list[str]:
    """Return the git parents of a commit already fetched into the mirror."""
    done = subprocess.run(
        ["git", "-C", str(mirror.store), "rev-list", "--parents", "-n", "1", sha],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.split()[1:]


def resolve(github: PullRequests, mirror: Mirror, number: int, head: str) -> tuple[str, str] | None:
    """Return the test merge GitHub built for a pull request and its first parent, fetched into the mirror, or None
    while GitHub has built no such merge yet or the one it names does not have the head as its second parent."""
    merge = str(github.json(f"pulls/{number}").get("merge_commit_sha") or "")
    if not merge:
        return None
    mirror.fetch(merge)
    parents = _parents(mirror, merge)
    if len(parents) != 2 or parents[1] != head:
        return None
    return merge, parents[0]


class Resolver:
    """The merge resolution of every repository the lab grades, through one GitHub reader and the scheduler's own
    mirror per repository."""

    def __init__(self, mirror: Callable[[str], Mirror], reader: Callable[[str], PullRequests] = Actions) -> None:
        self.mirror = mirror
        self.reader = reader
        self.readers: dict[str, PullRequests] = {}

    def __call__(self, repo: str, number: int, head: str) -> tuple[str, str] | None:
        """Return the resolved merge of a pull request of the owner/name repository and its head, or None."""
        if repo not in self.readers:
            self.readers[repo] = self.reader(repo)
        return resolve(self.readers[repo], self.mirror(repo), number, head)


class Background:
    """Merge resolutions run off the scheduler pass on at most `workers` threads at once. A call starts the
    resolution of a pull request's head and raises FetchPending while it still runs; a failed resolution then
    raises its error once. A resolution that found a merge answers every call until `fresh_s` after it started, so
    no merge stays in memory longer. One that found none answers until `absent_s` after it started, so the next
    pass tries again soon."""

    def __init__(
        self,
        resolve: Callable[[str, int, str], tuple[str, str] | None],
        workers: int = WORKERS,
        fresh_s: float = FRESH_S,
        clock: Callable[[], float] = time.monotonic,
        absent_s: float = ABSENT_S,
    ) -> None:
        self.resolve = resolve
        self.fresh_s = fresh_s
        self.absent_s = absent_s
        self.clock = clock
        self.pool = ThreadPoolExecutor(workers)
        self.resolutions: dict[tuple[str, int, str], tuple[float, Future[tuple[str, str] | None]]] = {}

    def __call__(self, repo: str, number: int, head: str) -> tuple[str, str] | None:
        """Return the finished resolution of a pull request's head, or raise MergePending while it runs and its
        error once it failed."""
        now = self.clock()
        self.resolutions = {
            key: (started, future)
            for key, (started, future) in self.resolutions.items()
            if not future.done() or now - started < self._kept_s(future)
        }
        key = (repo, number, head)
        if key not in self.resolutions:
            self.resolutions[key] = (now, self.pool.submit(self.resolve, repo, number, head))
        future = self.resolutions[key][1]
        if not future.done():
            raise MergePending(f"the merge of {repo}#{number} for {head} is still resolving")
        if future.exception() is not None:
            del self.resolutions[key]
        return future.result()

    def _kept_s(self, future: Future[tuple[str, str] | None]) -> float:
        """Return how long after its start a finished resolution keeps answering: the absent window when it found no
        merge, the fresh window when it found one or failed."""
        absent = future.exception() is None and future.result() is None
        return self.absent_s if absent else self.fresh_s
