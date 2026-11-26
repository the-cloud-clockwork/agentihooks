"""GitHub pull requests read and commented on through the gh CLI."""

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

from scripts.forge.model import ChangeRequest, Check, ForgeError

GH_TIMEOUT_SEC = 60
FIELDS = "number,url,title,body,headRefOid,files,statusCheckRollup"
SUBJECTS = '.commits[].commit.message | split("\\n")[0]'
QUEUED = re.compile(r"\(#(\d+)\)\s*$")
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

    def group_pulls(self, base: str, head: str) -> tuple[int, ...]:
        host, _, path = self.repo.partition("/")
        done = self.run(
            ["gh", "api", "--hostname", host, f"repos/{path}/compare/{base}...{head}", "--jq", SUBJECTS],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SEC,
        )
        if done.returncode:
            raise ForgeError(f"gh failed: {done.stderr.strip()}")
        found = (QUEUED.search(line) for line in done.stdout.splitlines())
        return tuple(dict.fromkeys(int(match.group(1)) for match in found if match))

    def read(self, number: int) -> ChangeRequest:
        raw = json.loads(self._gh(["pr", "view", str(number), "--json", FIELDS]))
        return ChangeRequest(
            number=raw["number"],
            url=raw["url"],
            title=raw["title"],
            body=raw["body"],
            head=raw["headRefOid"],
            diff=self._gh(["pr", "diff", str(number)]),
            files=tuple(item["path"] for item in raw["files"]),
            checks=tuple(check_of(item) for item in raw["statusCheckRollup"]),
        )

    def post(self, number: int, body: str) -> str:
        return self._gh(["pr", "comment", str(number), "--body-file", "-"], body).strip()
