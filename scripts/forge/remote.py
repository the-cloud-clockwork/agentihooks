"""Remote URL parsing, the origin reader and the picker that chooses a forge by remote host."""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from scripts.forge.github import GitHubForge, Runner
from scripts.forge.gitlab import GitLabForge
from scripts.forge.model import Forge, ForgeError

GIT_TIMEOUT_SEC = 30
TOKEN_ENV = "GITLAB_TOKEN"
KINDS = ("github", "gitlab")


@dataclass(frozen=True)
class Remote:
    host: str
    path: str


def parse(url: str) -> Remote:
    scheme, sep, rest = url.strip().partition("://")
    if not sep:
        rest = scheme.replace(":", "/")
    authority, _, path = rest.partition("/")
    host = authority.rpartition("@")[2].partition(":")[0].lower()
    path = path.strip("/").removesuffix(".git")
    if not host or "/" not in path:
        raise ValueError(f"not a repository remote: {url}")
    return Remote(host, path)


def kind_of(host: str) -> str:
    if host == "github.com":
        return "github"
    if "gitlab" in host.split("."):
        return "gitlab"
    raise ValueError(f"no forge known for host {host}; pass --forge github or gitlab")


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
