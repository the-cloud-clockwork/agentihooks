"""GitHub pull requests read and commented on through the gh CLI."""

import json
import subprocess
from collections.abc import Callable
from typing import Any

from scripts.forge.model import ChangeRequest, Check, ForgeError

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

    def _call(self, command: list[str], data: str | None = None) -> str:
        done = self.run(command, input=data, capture_output=True, text=True, timeout=GH_TIMEOUT_SEC)
        if done.returncode:
            raise ForgeError(f"gh failed: {done.stderr.strip()}")
        return done.stdout

    def _gh(self, args: list[str], data: str | None = None) -> str:
        return self._call(["gh", *args, "--repo", self.repo], data)

    def _api(self, path: str) -> Any:
        host, _, name = self.repo.partition("/")
        return json.loads(self._call(["gh", "api", "--hostname", host, f"repos/{name}/{path}"]))

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

    def release_pull(self, tag: str) -> int:
        sha = self._api(f"commits/{tag}")["sha"]
        found = [
            pull["number"]
            for pull in self._api(f"commits/{sha}/pulls")
            if pull["base"]["ref"] == "main" and pull["head"]["ref"].startswith("promotion/")
        ]
        if len(found) != 1:
            raise ForgeError(f"no single promotion pull request produced tag {tag}")
        return found[0]
