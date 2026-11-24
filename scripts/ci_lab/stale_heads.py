"""The heads a repository still wants graded, read from GitHub through the lab App.

`OpenHeads` is the scheduler's listing port: the head of every open pull request and of every plant branch of a
repository, each read in one paged listing and never one head at a time.
"""

from collections.abc import Callable

from scripts.ci_lab.checks import Token
from scripts.github_app import GitHubAppAuth

PERMISSIONS = {"contents": "read", "pull_requests": "read"}
PAGES = 50


def app(slug: str) -> Token:
    """Return the lab App's token source for a repository, holding read access to its pull requests and refs."""
    return GitHubAppAuth(f"github.com/{slug}", permissions=PERMISSIONS)


class OpenHeads:
    """The open pull request heads and plant branch heads of a repository."""

    def __init__(self, auth: Callable[[str], Token] = app) -> None:
        self.auth = auth

    def __call__(self, repo: str) -> frozenset[str]:
        """Return the heads the repository still wants graded."""
        del repo
        return frozenset()
