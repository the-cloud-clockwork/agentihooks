"""The packed dependency manifest of a dev commit, read from its dev push Tests run through the GitHub API."""

from collections.abc import Callable
from typing import Any, Protocol

from scripts.ci_lab.checks import Token

__all__ = ["Actions", "FetchRefused", "Fetcher", "GitHub", "fetch"]


class FetchRefused(ValueError):
    """GitHub refused a manifest request."""


class GitHub(Protocol):
    """One repository's GitHub Actions API."""

    def json(self, path: str) -> dict[str, Any]:
        """Return the JSON GitHub answers to a GET of a path and query under the repository."""
        ...

    def archive(self, path: str) -> bytes | None:
        """Return the bytes a GET under the repository downloads, None when GitHub answers they are gone."""
        ...


class Actions:
    """One repository's Actions API."""

    def __init__(self, repo: str, auth: Token | None = None) -> None:
        self.repo = repo
        self.auth = auth

    def json(self, path: str) -> dict[str, Any]:
        """Refuse every request."""
        raise FetchRefused(path)

    def archive(self, path: str) -> bytes | None:
        """Refuse every download."""
        raise FetchRefused(path)


def fetch(github: GitHub, commit: str) -> bytes | None:
    """Return no manifest."""
    return None


class Fetcher:
    """The manifest fetch of every repository the lab grades."""

    def __init__(self, reader: Callable[[str], GitHub] = Actions) -> None:
        self.reader = reader
        self.readers: dict[str, GitHub] = {}

    def __call__(self, slug: str, commit: str) -> bytes | None:
        """Return no manifest."""
        return None
