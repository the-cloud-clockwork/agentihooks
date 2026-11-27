import json
import re
from collections.abc import Mapping
from email.message import Message
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from scripts.forge import gitlab
from scripts.forge.model import ChangeRequest, Check, ForgeError
from tests.forge.fakes import API, HEADERS, Sent, fixture

pytestmark = pytest.mark.unit
DIFF = (
    "diff --git a/scripts/example.py b/scripts/example.py\n"
    "--- a/scripts/example.py\n"
    "+++ b/scripts/example.py\n"
    "@@ -1,2 +1,3 @@\n import os\n-old = 1\n+new = 1\n+more = 2\n"
    "diff --git a/tests/test_example.py b/tests/test_example.py\n"
    "--- /dev/null\n"
    "+++ b/tests/test_example.py\n"
    "@@ -0,0 +1,2 @@\n+def test_example():\n+    assert True\n"
    "diff --git a/docs/old.md b/docs/old.md\n"
    "--- a/docs/old.md\n"
    "+++ /dev/null\n"
    "@@ -1 +0,0 @@\n-gone\n"
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
        files=("scripts/example.py", "tests/test_example.py", "docs/old.md"),
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
    sent = Sent(merge, diffs[:2], diffs[:1])
    change = forge(sent).read(12)
    assert change.files == ("scripts/example.py", "tests/test_example.py", "scripts/example.py")
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


@pytest.mark.feature("forge-change-request-seam")
def test_gitlab_refuses_a_release_lookup_without_a_promotion_flow() -> None:
    sent = Sent()
    with pytest.raises(ForgeError, match="^GitLab has no promotion pull request for tag v1.2.3$"):
        forge(sent).release_pull("v1.2.3")
    assert sent.calls == []


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
    assert gitlab.urlopen_transport("PUT", f"{API}/12/notes", HEADERS, b"{}") == b'{"id": 1}'
    request = opened.request
    assert (request.get_method(), request.full_url, request.data, opened.timeout) == (
        "PUT",
        f"{API}/12/notes",
        b"{}",
        60,
    )
    assert request.header_items() == [("Private-token", "fake-token"), ("Content-type", "application/json")]


LINK = "https://gitlab.example.com/group/sub/repo/-/merge_requests/12"
HEAD = "5f0c2d1e9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d"
OLDER = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567"


def answer(state: str = "opened", status: str | None = "failed", sha: str = HEAD) -> dict[str, Any]:
    """Return the recorded merge request with its state and head pipeline set; status None drops the pipeline."""
    merge, _ = recorded()
    pipeline = None if status is None else {**merge["head_pipeline"], "status": status, "sha": sha}
    merge.update(state=state, head_pipeline=pipeline)
    return merge


def suite(conclusion: str) -> dict[str, dict[str, str]]:
    """Return the tracker suites of the recorded pipeline finished with ``conclusion``."""
    return {"501": {"app": "gitlab-ci", "conclusion": conclusion}}


class Refused:
    """A fake transport that answers every request with one HTTP error status and records each URL."""

    def __init__(self, code: int) -> None:
        self.code = code
        self.calls: list[str] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str], data: bytes | None) -> bytes:
        self.calls.append(url)
        raise HTTPError(url, self.code, "refused", Message(), None)


class Answered(Opened):
    """A fake urlopen that answers the recorded merge request."""

    def read(self) -> bytes:
        merge, _ = recorded()
        return json.dumps(merge).encode()


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_an_opened_merge_request_reads_open_with_its_failed_pipeline_as_one_suite() -> None:
    sent = Sent(answer())
    assert gitlab.merge_request_state(LINK, "fake-token", sent) == gitlab.MergeRequestState(
        url=LINK, state="open", head=HEAD, queued=False, checks_head=HEAD, suites=suite("failure")
    )
    assert sent.calls == [("GET", f"{API}/12", HEADERS, None)]


@pytest.mark.feature("forge-gitlab-merge-request-state")
@pytest.mark.parametrize(
    ("state", "status", "expected", "conclusion"),
    [
        ("merged", "success", "merged", "success"),
        ("closed", "canceled", "closed", "cancelled"),
        ("opened", "skipped", "open", "skipped"),
    ],
    ids=["merged-success", "closed-canceled", "opened-skipped"],
)
def test_merge_request_states_and_finished_pipelines_read_in_the_tracker_words(
    state: str, status: str, expected: str, conclusion: str
) -> None:
    read = gitlab.merge_request_state(LINK, "fake-token", Sent(answer(state, status)))
    assert (read.state, read.head, read.queued, read.checks_head, read.suites) == (
        expected,
        HEAD,
        False,
        HEAD,
        suite(conclusion),
    )


@pytest.mark.feature("forge-gitlab-merge-request-state")
@pytest.mark.parametrize("status", ["running", "pending", "created", "manual"])
def test_an_unfinished_pipeline_gives_no_suite_on_its_own_commit(status: str) -> None:
    read = gitlab.merge_request_state(LINK, "fake-token", Sent(answer(status=status, sha=OLDER)))
    assert (read.head, read.checks_head, read.suites) == (HEAD, OLDER, {})


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_a_merge_request_without_a_pipeline_reads_its_head_with_no_suites() -> None:
    read = gitlab.merge_request_state(LINK, "fake-token", Sent(answer(status=None)))
    assert read == gitlab.MergeRequestState(LINK, "open", HEAD, False, HEAD, {})


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_a_finished_pipeline_on_an_older_commit_keeps_that_commit_as_its_checks_head() -> None:
    read = gitlab.merge_request_state(LINK, "fake-token", Sent(answer(status="success", sha=OLDER)))
    assert (read.head, read.checks_head, read.suites) == (HEAD, OLDER, suite("success"))


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_a_merge_request_in_another_state_raises_a_fetch_error() -> None:
    with pytest.raises(gitlab.FetchError, match=f"^GitLab merge request {re.escape(LINK)} is locked$"):
        gitlab.merge_request_state(LINK, "fake-token", Sent(answer(state="locked")))


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_a_missing_merge_request_raises_a_fetch_error_never_a_closed_state() -> None:
    refused = Refused(404)
    with pytest.raises(gitlab.FetchError, match=f"^GitLab has no merge request at {re.escape(LINK)}$") as raised:
        gitlab.merge_request_state(LINK, "fake-token", refused)
    assert isinstance(raised.value, ForgeError)
    assert isinstance(raised.value.__cause__, HTTPError)
    assert refused.calls == [f"{API}/12"]


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_another_http_error_reaches_the_caller_as_it_came() -> None:
    with pytest.raises(HTTPError) as raised:
        gitlab.merge_request_state(LINK, "fake-token", Refused(500))
    assert raised.value.code == 500


@pytest.mark.feature("forge-gitlab-merge-request-state")
@pytest.mark.parametrize("token", ["", None], ids=["empty", "missing"])
def test_a_missing_token_raises_a_fetch_error_before_any_request(token: str | None) -> None:
    sent = Sent()
    with pytest.raises(gitlab.FetchError, match="^GITLAB_TOKEN is not set$"):
        gitlab.merge_request_state(LINK, token, sent)
    assert sent.calls == []


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_a_github_pull_link_raises_a_fetch_error_before_any_request() -> None:
    sent = Sent()
    pull = "https://github.com/owner/repo/pull/7"
    with pytest.raises(gitlab.FetchError, match=f"^not a GitLab merge request link: {re.escape(pull)}$"):
        gitlab.merge_request_state(pull, "fake-token", sent)
    assert sent.calls == []


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_a_link_of_no_forge_raises_a_fetch_error_before_any_request() -> None:
    sent = Sent()
    foreign = "https://example.com/group/repo/-/merge_requests/3"
    with pytest.raises(gitlab.FetchError, match=f"^not a GitLab merge request link: {re.escape(foreign)}$") as raised:
        gitlab.merge_request_state(foreign, "fake-token", sent)
    assert isinstance(raised.value.__cause__, ValueError)
    assert sent.calls == []


@pytest.mark.feature("forge-gitlab-merge-request-state")
def test_the_state_read_sends_through_urlopen_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    answered = Answered()
    monkeypatch.setattr(gitlab, "urlopen", answered)
    assert gitlab.merge_request_state(LINK, "fake-token").state == "open"
    assert (answered.request.get_method(), answered.request.full_url) == ("GET", f"{API}/12")
