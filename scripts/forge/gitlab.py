"""GitLab merge requests read and commented on through the REST v4 API."""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from scripts.forge.model import ChangeRequest, Check, ForgeError

__all__ = [
    "CONCLUSIONS",
    "HTTP_TIMEOUT_SEC",
    "PAGE_SIZE",
    "PIPELINE_APP",
    "STATES",
    "FetchError",
    "GitLabForge",
    "MergeRequestState",
    "Transport",
    "merge_request_state",
    "pipeline_suites",
    "unified",
    "urlopen_transport",
]

HTTP_TIMEOUT_SEC = 60
PAGE_SIZE = 100
STATES = {"opened": "open", "closed": "closed", "merged": "merged"}
CONCLUSIONS = {"success": "success", "failed": "failure", "canceled": "cancelled", "skipped": "skipped"}
PIPELINE_APP = "gitlab-ci"
Transport = Callable[[str, str, Mapping[str, str], bytes | None], bytes]


def urlopen_transport(method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> bytes:
    with urlopen(Request(url, data=data, headers=dict(headers), method=method), timeout=HTTP_TIMEOUT_SEC) as response:
        return response.read()


def unified(item: dict[str, Any]) -> str:
    old = "/dev/null" if item["new_file"] else f"a/{item['old_path']}"
    new = "/dev/null" if item["deleted_file"] else f"b/{item['new_path']}"
    return f"diff --git a/{item['old_path']} b/{item['new_path']}\n--- {old}\n+++ {new}\n{item['diff']}"


class FetchError(ForgeError):
    """A GitLab merge request state could not be read; the tracker counts it as a failed fetch, never a closed pull."""


@dataclass(frozen=True)
class MergeRequestState:
    url: str
    state: str
    head: str
    queued: bool
    checks_head: str
    suites: dict[str, dict[str, str]]


def pipeline_suites(pipeline: dict[str, Any]) -> dict[str, dict[str, str]]:
    raise NotImplementedError


class GitLabForge:
    def __init__(self, host: str, path: str, token: str, send: Transport = urlopen_transport) -> None:
        self.web = f"https://{host}/{path}"
        self.api = f"https://{host}/api/v4/projects/{path.replace('/', '%2F')}/merge_requests"
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
            files=tuple(item["new_path"] for item in diffs),
            checks=(Check("pipeline", pipeline["status"]),) if pipeline else (),
        )

    def post(self, number: int, body: str) -> str:
        note = self._call("POST", f"{self.api}/{number}/notes", {"body": body})
        return f"{self.web}/-/merge_requests/{number}#note_{note['id']}"

    def state(self, number: int) -> MergeRequestState:
        raise NotImplementedError


def merge_request_state(url: str, token: str | None, send: Transport = urlopen_transport) -> MergeRequestState:
    raise NotImplementedError
