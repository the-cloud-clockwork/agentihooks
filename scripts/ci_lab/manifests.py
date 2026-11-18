"""The packed dependency manifest of a dev commit, read from its dev push Tests run through the GitHub API.

`fetch` finds the dev push run of the Tests workflow for one commit, lists that run's `dep-manifest-packed` artifact
and downloads its archive. A commit with no such run or artifact, an expired artifact, an archive GitHub answers is
gone and a manifest recorded for another commit answer None; a refused request or an archive without a readable
`packed.json` raises `FetchRefused`. `Actions` reads one repository's Actions API with a token the lab's GitHub App
scopes to actions read, and `Fetcher` keeps one reader per repository the lab grades.
"""

import io
import json
import zipfile
from collections.abc import Callable
from typing import Any, Protocol

import requests

from scripts.ci_lab.checks import Token
from scripts.github_app import GitHubAppAuth

__all__ = ["ARTIFACT", "PERMISSIONS", "Actions", "FetchRefused", "Fetcher", "GitHub", "fetch"]

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
PERMISSIONS = {"actions": "read"}
TIMEOUT = 30
OK = 200
GONE = 410
RUNS = "actions/workflows/test.yml/runs?event=push&branch=dev&head_sha="
ARTIFACT = "dep-manifest-packed"
LISTED = f"artifacts?name={ARTIFACT}"
PACKED = "packed.json"


class FetchRefused(ValueError):
    """GitHub refused a manifest request, or sent an archive that holds no readable packed manifest."""


class GitHub(Protocol):
    """One repository's GitHub Actions API."""

    def json(self, path: str) -> dict[str, Any]:
        """Return the JSON GitHub answers to a GET of a path and query under the repository."""
        ...

    def archive(self, path: str) -> bytes | None:
        """Return the bytes a GET under the repository downloads, None when GitHub answers they are gone."""
        ...


def _accepted(response: requests.Response, path: str) -> requests.Response:
    """Return an answer GitHub accepted, or raise FetchRefused naming the request and its status."""
    if response.status_code != OK:
        raise FetchRefused(f"GET {path} answered {response.status_code}")
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
        """Send one GET under the repository with a fresh token; requests drops it on the archive's storage redirect."""
        return requests.get(
            f"{API}/repos/{self.repo}/{path}",
            headers={"Authorization": f"Bearer {self.auth.get_token()}", **HEADERS},
            timeout=TIMEOUT,
        )


def _packed(data: bytes, path: str) -> bytes:
    """Return the packed manifest file of an artifact archive, or raise FetchRefused naming the archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.read(PACKED)
    except (zipfile.BadZipFile, KeyError) as error:
        raise FetchRefused(f"{path} holds no readable {PACKED}") from error


def fetch(github: GitHub, commit: str) -> bytes | None:
    """Return the packed manifest file the dev push Tests run of a commit kept, None when no run kept a live one."""
    for run in github.json(f"{RUNS}{commit}")["workflow_runs"]:
        for artifact in github.json(f"actions/runs/{run['id']}/{LISTED}")["artifacts"]:
            path = f"actions/artifacts/{artifact['id']}/zip"
            data = None if artifact["expired"] else github.archive(path)
            if data is None:
                continue
            packed = _packed(data, path)
            if json.loads(packed)["commit"] == commit:
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
