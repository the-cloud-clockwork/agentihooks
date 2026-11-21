"""GitHub pull requests read and commented on through the gh CLI."""

import json
import subprocess
from collections.abc import Callable
from typing import Any

from scripts.forge.model import ChangedFile, ChangeRequest, Check, ForgeError

GH_TIMEOUT_SEC = 60
FIELDS = "number,url,title,body,headRefOid,files,statusCheckRollup"
Runner = Callable[..., subprocess.CompletedProcess[str]]


def check_of(item: dict[str, Any]) -> Check:
    if item["__typename"] == "StatusContext":
        return Check(item["context"], item["state"])
    return Check(item["name"], item["conclusion"] or item["status"])


class GitHubForge:
    def __init__(self, repo: str, run: Runner = subprocess.run) -> None:
        self.repo = repo
        self.run = run

    def _gh(self, args: list[str], data: str | None = None) -> str:
        done = self.run(
            ["gh", *args, "--repo", self.repo], input=data, capture_output=True, text=True, timeout=GH_TIMEOUT_SEC
        )
        if done.returncode:
            raise ForgeError(f"gh failed: {done.stderr.strip()}")
        return done.stdout

    def read(self, number: int) -> ChangeRequest:
        raw = json.loads(self._gh(["pr", "view", str(number), "--json", FIELDS]))
        return ChangeRequest(
            number=raw["number"],
            url=raw["url"],
            title=raw["title"],
            body=raw["body"],
            head=raw["headRefOid"],
            diff=self._gh(["pr", "diff", str(number)]),
            files=tuple(ChangedFile(item["path"], item["additions"], item["deletions"]) for item in raw["files"]),
            checks=tuple(check_of(item) for item in raw["statusCheckRollup"]),
        )

    def post(self, number: int, body: str) -> str:
        return self._gh(["pr", "comment", str(number), "--body-file", "-"], body).strip()
