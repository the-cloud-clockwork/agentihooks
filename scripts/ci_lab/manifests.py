"""The packed dependency manifest of a dev commit, read from its dev push Tests run through the GitHub API.

`fetch` finds the dev push run of the Tests workflow for one commit, lists that run's `dep-manifest-packed` artifact
and downloads its archive. A commit with no such run or artifact, an expired artifact, an archive GitHub answers is
gone and a manifest recorded for another commit answer None; a refused or unanswered request or an archive without a
readable `packed.json` raises `FetchRefused`, and a request GitHub never answered or answered with a server error raises
its subclass `FetchUnanswered`. `Actions` reads one repository's Actions API with a token the lab's
GitHub App scopes to actions read, and `Fetcher` keeps one reader per repository the lab grades. `Background` runs
those fetches off the scheduler pass and answers only finished ones, raising `FetchPending` while one still runs.
"""

import io
import json
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol

import requests

from scripts.ci_lab.checks import Token
from scripts.github_app import GitAuthRefused, GitHubAppAuth

__all__ = [
    "ARTIFACT",
    "PERMISSIONS",
    "Actions",
    "Background",
    "FetchPending",
    "FetchRefused",
    "FetchUnanswered",
    "Fetcher",
    "GitHub",
    "fetch",
]

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
PERMISSIONS = {"actions": "read"}
TIMEOUT = 30
OK = 200
GONE = 410
SERVER_ERROR = 500
DEV_PUSH_RUNS_PATH = "actions/workflows/test.yml/runs?event=push&branch=dev&head_sha="
ARTIFACT = "dep-manifest-packed"
ARTIFACTS_BY_NAME_QUERY = f"artifacts?name={ARTIFACT}"
PACKED = "packed.json"
WORKERS = 4
FRESH_S = 300.0


class FetchRefused(ValueError):
    """GitHub refused or never answered a manifest request, or sent an archive without a readable packed manifest."""


class FetchUnanswered(FetchRefused):
    """GitHub never answered a manifest request or answered it with a server error, so a later request may succeed."""


class FetchPending(FetchUnanswered):
    """A manifest fetch still runs off the scheduler pass, so a later call answers it."""


class GitHub(Protocol):
    """One repository's GitHub Actions API."""

    def json(self, path: str) -> dict[str, Any]:
        """Return the JSON GitHub answers to a GET of a path and query under the repository."""
        ...

    def archive(self, path: str) -> bytes | None:
        """Return the bytes a GET under the repository downloads, None when GitHub answers they are gone."""
        ...


def _accepted(response: requests.Response, path: str) -> requests.Response:
    """Return an answer GitHub accepted, or raise FetchRefused naming the request and its status, FetchUnanswered for a
    server error."""
    if response.status_code != OK:
        refused = FetchUnanswered if response.status_code >= SERVER_ERROR else FetchRefused
        raise refused(f"GET {path} answered {response.status_code}")
    return response


class Actions:
    """One repository's Actions API, read with a token the lab's GitHub App scopes to actions read."""

    def __init__(self, repo: str, auth: Token | None = None) -> None:
        self.repo = repo
        self.auth: Token = auth if auth is not None else GitHubAppAuth(f"github.com/{repo}", permissions=PERMISSIONS)

    def json(self, path: str) -> dict[str, Any]:
        """Return the JSON of an accepted answer, or raise FetchRefused."""
        data: dict[str, Any] = _accepted(self._get(path), path).json()
        return data

    def archive(self, path: str) -> bytes | None:
        """Return the downloaded bytes, None when GitHub answers they are gone, or raise FetchRefused."""
        response = self._get(path)
        return None if response.status_code == GONE else _accepted(response, path).content

    def _get(self, path: str) -> requests.Response:
        """Send one GET under the repository with a fresh token; requests drops it on the archive's storage redirect.
        A request that fails before GitHub answers it, a token mint without an answer included, raises FetchUnanswered
        naming it."""
        try:
            return requests.get(
                f"{API}/repos/{self.repo}/{path}",
                headers={"Authorization": f"Bearer {self.auth.get_token()}", **HEADERS},
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            raise FetchUnanswered(f"GET {path} failed: {error}") from error
        except GitAuthRefused as error:
            refused = (
                FetchUnanswered if error.status_code is not None and error.status_code >= SERVER_ERROR else FetchRefused
            )
            raise refused(f"GET {path} token mint failed: {error}") from error


def _read_packed(data: bytes, path: str) -> tuple[bytes, Any]:
    """Return the packed manifest file of an artifact archive and the commit it records, or raise FetchRefused naming
    the archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            packed = archive.read(PACKED)
        return packed, json.loads(packed)["commit"]
    except (zipfile.BadZipFile, KeyError, TypeError, ValueError) as error:
        raise FetchRefused(f"{path} holds no readable {PACKED}") from error


def fetch(github: GitHub, commit: str) -> bytes | None:
    """Return the packed manifest file the dev push Tests run of a commit kept, None when no run kept a live one."""
    for run in github.json(f"{DEV_PUSH_RUNS_PATH}{commit}")["workflow_runs"]:
        for artifact in github.json(f"actions/runs/{run['id']}/{ARTIFACTS_BY_NAME_QUERY}")["artifacts"]:
            path = f"actions/artifacts/{artifact['id']}/zip"
            data = None if artifact["expired"] else github.archive(path)
            if data is None:
                continue
            packed, recorded = _read_packed(data, path)
            if recorded == commit:
                return packed
    return None


class Fetcher:
    """The manifest fetch of every repository the lab grades, through one reader per repository."""

    def __init__(self, reader: Callable[[str], GitHub] = Actions) -> None:
        self.reader = reader
        self.readers: dict[str, GitHub] = {}

    def __call__(self, slug: str, commit: str) -> bytes | None:
        """Return the packed manifest of a dev commit of the owner/name repository, or None."""
        if slug not in self.readers:
            self.readers[slug] = self.reader(slug)
        return fetch(self.readers[slug], commit)


class Background:
    """Manifest fetches run off the scheduler pass on at most `workers` threads at once. A call starts the fetch of a
    repository's commit and raises FetchPending until it finished; a failed fetch then raises its error once, and any
    other answers every call until `fresh_s` after the fetch started, so a search that passes over a commit holding no
    manifest does not fetch it again on every pass."""

    def __init__(
        self,
        fetch: Callable[[str, str], bytes | None],
        workers: int = WORKERS,
        fresh_s: float = FRESH_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fetch = fetch
        self.fresh_s = fresh_s
        self.clock = clock
        self.pool = ThreadPoolExecutor(workers)
        self.fetches: dict[tuple[str, str], tuple[float, Future[bytes | None]]] = {}

    def __call__(self, slug: str, commit: str) -> bytes | None:
        """Return the finished fetch of a dev commit of the owner/name repository, or raise FetchPending while it runs
        and its error once it failed."""
        now = self.clock()
        self.fetches = {
            key: (started, future)
            for key, (started, future) in self.fetches.items()
            if not future.done() or now - started < self.fresh_s
        }
        key = (slug, commit)
        if key not in self.fetches:
            self.fetches[key] = (now, self.pool.submit(self.fetch, slug, commit))
        future = self.fetches[key][1]
        if not future.done():
            raise FetchPending(f"{slug} manifest of {commit} is still fetching")
        if future.exception() is not None:
            del self.fetches[key]
        return future.result()
