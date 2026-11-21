"""GitLab merge requests read and commented on through the REST v4 API."""

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from scripts.forge.model import ChangedFile, ChangeRequest, Check

HTTP_TIMEOUT_SEC = 60
PAGE_SIZE = 100
Transport = Callable[[str, str, Mapping[str, str], bytes | None], bytes]


def urlopen_transport(method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> bytes:
    with urlopen(Request(url, data=data, headers=dict(headers), method=method), timeout=HTTP_TIMEOUT_SEC) as response:
        return response.read()


def changed(item: dict[str, Any]) -> ChangedFile:
    lines: list[str] = item["diff"].splitlines()
    added = sum(line.startswith("+") for line in lines)
    return ChangedFile(item["new_path"], added, sum(line.startswith("-") for line in lines))


def unified(item: dict[str, Any]) -> str:
    old, new = item["old_path"], item["new_path"]
    return f"diff --git a/{old} b/{new}\n--- a/{old}\n+++ b/{new}\n{item['diff']}"


class GitLabForge:
    def __init__(self, host: str, path: str, token: str, send: Transport = urlopen_transport) -> None:
        self.web = f"https://{host}/{path}"
        self.api = f"https://{host}/api/v4/projects/{quote(path, safe='')}/merge_requests"
        self.token = token
        self.send = send

    def _call(self, method: str, url: str, payload: dict[str, str] | None = None) -> Any:
        headers = {"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"}
        data = None if payload is None else json.dumps(payload).encode()
        return json.loads(self.send(method, url, headers, data))

    def _diffs(self, number: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._call("GET", f"{self.api}/{number}/diffs?per_page={PAGE_SIZE}&page={page}")
            found.extend(batch)
            if len(batch) < PAGE_SIZE:
                return found
            page += 1

    def read(self, number: int) -> ChangeRequest:
        raw = self._call("GET", f"{self.api}/{number}")
        diffs = self._diffs(number)
        pipeline = raw["head_pipeline"]
        return ChangeRequest(
            number=raw["iid"],
            url=raw["web_url"],
            title=raw["title"],
            body=raw["description"] or "",
            head=raw["sha"],
            diff="".join(unified(item) for item in diffs),
            files=tuple(changed(item) for item in diffs),
            checks=(Check("pipeline", pipeline["status"]),) if pipeline else (),
        )

    def post(self, number: int, body: str) -> str:
        note = self._call("POST", f"{self.api}/{number}/notes", {"body": body})
        return f"{self.web}/-/merge_requests/{number}#note_{note['id']}"
