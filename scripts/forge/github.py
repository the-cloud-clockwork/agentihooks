"""GitHub pull requests read and commented on through the gh CLI."""

import subprocess
from collections.abc import Callable
from typing import Any

from scripts.forge.model import ChangeRequest, Check

GH_TIMEOUT_SEC = 60
FIELDS = "number,url,title,body,headRefOid,files,statusCheckRollup"
Runner = Callable[..., subprocess.CompletedProcess[str]]


def check_of(item: dict[str, Any]) -> Check:
    raise NotImplementedError


class GitHubForge:
    def __init__(self, repo: str, run: Runner = subprocess.run) -> None:
        self.repo = repo
        self.run = run

    def read(self, number: int) -> ChangeRequest:
        raise NotImplementedError

    def post(self, number: int, body: str) -> str:
        raise NotImplementedError
