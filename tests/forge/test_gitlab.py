import json
from typing import Any
from urllib.request import Request

import pytest

from scripts.forge import gitlab
from scripts.forge.model import ChangedFile, ChangeRequest, Check
from tests.forge.fakes import API, HEADERS, Sent, fixture

pytestmark = pytest.mark.unit
DIFF = (
    "diff --git a/scripts/example.py b/scripts/example.py\n"
    "--- a/scripts/example.py\n"
    "+++ b/scripts/example.py\n"
    "@@ -1,2 +1,3 @@\n import os\n-old = 1\n+new = 1\n+more = 2\n"
    "diff --git a/tests/test_example.py b/tests/test_example.py\n"
    "--- a/tests/test_example.py\n"
    "+++ b/tests/test_example.py\n"
    "@@ -0,0 +1,2 @@\n+def test_example():\n+    assert True\n"
)


def forge(sent: Sent) -> gitlab.GitLabForge:
    """Return a GitLab forge for the recorded project that talks through ``sent``."""
    return gitlab.GitLabForge("gitlab.example.com", "group/sub/repo", "fake-token", sent)


def recorded() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the recorded merge request and its diffs page."""
    return json.loads(fixture("gitlab_mr.json")), json.loads(fixture("gitlab_diffs.json"))


@pytest.mark.feature("forge-change-request-seam")
def test_gitlab_reads_a_recorded_merge_request_into_a_change_request() -> None:
    merge, diffs = recorded()
    sent = Sent(merge, diffs)
    assert forge(sent).read(12) == ChangeRequest(
        number=12,
        url="https://gitlab.example.com/group/sub/repo/-/merge_requests/12",
        title="Add the example",
        body="Reviewer seats read the change through the forge.",
        head="5f0c2d1e9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d",
        diff=DIFF,
        files=(ChangedFile("scripts/example.py", 2, 1), ChangedFile("tests/test_example.py", 2, 0)),
        checks=(Check("pipeline", "failed"),),
    )
    assert sent.calls == [
        ("GET", f"{API}/12", HEADERS, None),
        ("GET", f"{API}/12/diffs?per_page=100&page=1", HEADERS, None),
    ]


@pytest.mark.feature("forge-change-request-seam")
def test_gitlab_reads_every_diffs_page_until_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab, "PAGE_SIZE", 2)
    merge, diffs = recorded()
    sent = Sent(merge, diffs, diffs[:1])
    change = forge(sent).read(12)
    assert [item.path for item in change.files] == ["scripts/example.py", "tests/test_example.py", "scripts/example.py"]
    assert [url for _, url, _, _ in sent.calls[1:]] == [
        f"{API}/12/diffs?per_page=2&page=1",
        f"{API}/12/diffs?per_page=2&page=2",
    ]


@pytest.mark.feature("forge-change-request-seam")
def test_a_merge_request_without_pipeline_or_description_reads_empty() -> None:
    merge, _ = recorded()
    merge.update(head_pipeline=None, description=None)
    change = forge(Sent(merge, [])).read(12)
    assert (change.body, change.diff, change.files, change.checks) == ("", "", (), ())


@pytest.mark.feature("forge-change-request-seam")
def test_gitlab_posts_a_review_comment_as_a_merge_request_note() -> None:
    sent = Sent({"id": 3301, "body": "Finding one"})
    url = forge(sent).post(12, "Finding one")
    assert url == "https://gitlab.example.com/group/sub/repo/-/merge_requests/12#note_3301"
    assert sent.calls == [("POST", f"{API}/12/notes", HEADERS, b'{"body": "Finding one"}')]


class Opened:
    """A fake urlopen that records the request and answers a fixed body."""

    request: Request
    timeout: float

    def __call__(self, request: Request, timeout: float) -> "Opened":
        self.request, self.timeout = request, timeout
        return self

    def __enter__(self) -> "Opened":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return b'{"id": 1}'


@pytest.mark.feature("forge-change-request-seam")
def test_the_default_transport_sends_one_request_through_urlopen(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = Opened()
    monkeypatch.setattr(gitlab, "urlopen", opened)
    assert gitlab.urlopen_transport("POST", f"{API}/12/notes", HEADERS, b"{}") == b'{"id": 1}'
    request = opened.request
    assert (request.get_method(), request.full_url, request.data, opened.timeout) == (
        "POST",
        f"{API}/12/notes",
        b"{}",
        60,
    )
    assert request.header_items() == [("Private-token", "fake-token"), ("Content-type", "application/json")]
