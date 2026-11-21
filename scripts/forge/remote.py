"""Remote URL parsing, the origin reader and the picker that chooses a forge by remote host."""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from scripts.forge.github import Runner
from scripts.forge.model import Forge

GIT_TIMEOUT_SEC = 30
TOKEN_ENV = "GITLAB_TOKEN"
KINDS = ("github", "gitlab")


@dataclass(frozen=True)
class Remote:
    host: str
    path: str


def parse(url: str) -> Remote:
    raise NotImplementedError


def kind_of(host: str) -> str:
    raise NotImplementedError


def pick(where: Remote, kind: str | None, env: Mapping[str, str]) -> Forge:
    raise NotImplementedError


def origin(directory: str, run: Runner = subprocess.run) -> str:
    raise NotImplementedError
