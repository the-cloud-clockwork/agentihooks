"""The heads a repository still wants graded, and the live pull request grades that are none of them.

`OpenHeads` is the scheduler's listing port, read from GitHub through the lab App: the head of every open pull
request and of every plant branch of a repository, each from a paged listing read twice and never one head at a time.
`repositories` names the repositories the first pass of a scheduler lists, `stale` the grades it then retires and
`listed` the line that counts them.
"""

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

from scripts.ci_lab.checks import API, HEADERS, TIMEOUT, Token
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import PLANT_PREFIX
from scripts.github_app import GitHubAppAuth

PERMISSIONS = {"contents": "read", "pull_requests": "read"}
PULLS = "pulls"
PLANTS = f"git/matching-refs/{PLANT_PREFIX.removeprefix('refs/')}"
PER_PAGE = 100
PAGES = 50

Origins = Mapping[str, Mapping[str, Any]]


def app(slug: str) -> Token:
    """Return the lab App's token source for a repository, holding read access to its pull requests and refs."""
    return GitHubAppAuth(f"github.com/{slug}", permissions=PERMISSIONS)


def _pages(slug: str, path: str, query: Mapping[str, str], auth: Token) -> list[Any]:
    """Return every row of one GitHub listing, following its next links; raise when GitHub refuses a page or the
    listing is longer than `PAGES` pages, so a caller never takes a part of it for the whole."""
    rows: list[Any] = []
    url = f"{API}/repos/{slug}/{path}?{urlencode({**query, 'per_page': PER_PAGE})}"
    for _ in range(PAGES):
        headers = {"Authorization": f"Bearer {auth.get_token()}", **HEADERS}
        response = requests.get(url, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        rows.extend(response.json())
        if "next" not in response.links:
            return rows
        url = response.links["next"]["url"]
    raise OSError(f"{path} of {slug} holds more than {PAGES} pages")


def _heads(slug: str, auth: Token) -> frozenset[str]:
    """Return the open pull request heads and the plant branch heads one reading of the two listings names."""
    pulls = _pages(slug, PULLS, {"state": "open"}, auth)
    plants = _pages(slug, PLANTS, {}, auth)
    return frozenset([*(pull["head"]["sha"] for pull in pulls), *(plant["object"]["sha"] for plant in plants)])


class OpenHeads:
    """The open pull request heads and plant branch heads of a repository."""

    def __init__(self, auth: Callable[[str], Token] = app) -> None:
        self.auth = auth

    def __call__(self, repo: str) -> frozenset[str]:
        """Return the heads the repository still wants graded. The listings are read twice and a head either
        reading names is wanted, since a pull request closed between two pages moves a later one out of a single
        reading. Whatever keeps a listing from being read or understood is raised as an OSError."""
        slug = urlsplit(repo).path.removeprefix("/")
        try:
            auth = self.auth(slug)
            return _heads(slug, auth) | _heads(slug, auth)
        except (KeyError, TypeError, ValueError) as error:
            raise OSError(f"the listings of {slug} cannot be read: {error!r}") from error


def _plain(origin: Mapping[str, Any]) -> bool:
    """Whether a live grade is a pull request grade GitHub delivered, not one pushed to a grade ref."""
    return origin["tier"] == Priority.PULL_REQUEST and not origin["ref"]


def repositories(origins: Origins) -> list[str]:
    """Return, in order, the repositories with a live pull request grade that has no grade ref."""
    return sorted({origin["repo"] for origin in origins.values() if _plain(origin)})


def stale(origins: Origins, wanted: Mapping[str, frozenset[str]]) -> dict[str, tuple[str, str]]:
    """Return the repository and head of every live pull request grade with no grade ref whose head its repository's
    listing does not hold; a repository with no listing has no stale grade."""
    return {
        run: (origin["repo"], origin["head"])
        for run, origin in origins.items()
        if _plain(origin) and origin["repo"] in wanted and origin["head"] not in wanted[origin["repo"]]
    }


def listed(repo: str, heads: int, origins: Origins, gone: Mapping[str, tuple[str, str]], dropped: int) -> str:
    """Return the line counting one repository's first pass: the heads listed, its live pull request grades, the
    grades retired and the pending units dropped."""
    live = sum(1 for origin in origins.values() if _plain(origin) and origin["repo"] == repo)
    retired = sum(1 for name, _ in gone.values() if name == repo)
    return (
        f"listed {heads} wanted heads of {repo}: {live} live pull request grades, {retired} retired, "
        f"{dropped} pending units dropped"
    )
