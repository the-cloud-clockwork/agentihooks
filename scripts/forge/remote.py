"""Remote URL and change request link parsing, the origin reader and the picker that chooses a forge by remote host."""

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from scripts.forge.github import GitHubForge, Runner
from scripts.forge.gitlab import GitLabForge
from scripts.forge.model import Forge, ForgeError

__all__ = [
    "CHANGE_LINK",
    "GIT_TIMEOUT_SEC",
    "KINDS",
    "MARKERS",
    "TOKEN_ENV",
    "ChangeLink",
    "Remote",
    "change_link",
    "kind_of",
    "origin",
    "parse",
    "pick",
]

GIT_TIMEOUT_SEC = 30
TOKEN_ENV = "GITLAB_TOKEN"
KINDS = ("github", "gitlab")
CHANGE_LINK = re.compile(
    r"https://(?:github\.com/[\w.-]+/[\w.-]+/pull"
    r"|(?:[\w-]+\.)*gitlab(?:\.[\w-]+)*/[\w.][\w.-]*(?:/[\w.][\w.-]*)+/-/merge_requests)/[1-9][0-9]*"
)
MARKERS = {"github": "/pull/", "gitlab": "/-/merge_requests/"}


@dataclass(frozen=True)
class Remote:
    host: str
    path: str


@dataclass(frozen=True)
class ChangeLink:
    kind: str
    host: str
    path: str
    number: int
    web: str
    clone: str


def parse(url: str) -> Remote:
    scheme, sep, rest = url.strip().partition("://")
    if not sep:
        rest = scheme.replace(":", "/")
    authority, _, path = rest.partition("/")
    host = authority.rpartition("@")[2].partition(":")[0].lower()
    path = path.strip("/").removesuffix(".git")
    if not host or "/" not in path:
        raise ValueError(f"not a repository remote: {host}/{path}")
    return Remote(host, path)


def kind_of(host: str) -> str:
    if host == "github.com":
        return "github"
    if "gitlab" in host.split("."):
        return "gitlab"
    raise ValueError(f"no forge known for host {host}; pass --forge github or gitlab")


def change_link(url: str) -> ChangeLink:
    """Read a GitHub pull link or a GitLab merge request link into its forge, project and number.

    Only the exact link is accepted: https, a lowercase host without a port or credentials, no trailing slash, sub
    page, query or fragment, and a number without a leading zero. A GitLab host must carry a gitlab label, the rule
    kind_of applies, and a GitLab project path keeps its nested groups whole. CHANGE_LINK accepts the same links.
    Anything else raises ValueError.
    """
    if not CHANGE_LINK.fullmatch(url):
        raise ValueError("not a GitHub pull link or GitLab merge request link")
    host, _, rest = url.removeprefix("https://").partition("/")
    kind = kind_of(host)
    path, _, number = rest.rpartition(MARKERS[kind])
    web = f"https://{host}/{path}"
    return ChangeLink(kind, host, path, int(number), web, f"{web}.git")


def pick(where: Remote, kind: str | None, env: Mapping[str, str]) -> Forge:
    if (kind or kind_of(where.host)) == "github":
        return GitHubForge(f"{where.host}/{where.path}")
    token = env.get(TOKEN_ENV)
    if not token:
        raise ValueError(f"{TOKEN_ENV} is not set")
    return GitLabForge(where.host, where.path, token)


def origin(directory: str, run: Runner = subprocess.run) -> str:
    done = run(
        ["git", "-C", directory, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC
    )
    if done.returncode:
        raise ForgeError(f"no origin remote in {directory}")
    return done.stdout.strip()
