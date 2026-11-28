"""Swarm reads of a GitLab merge request: the pull request view and the intent gate head and merged reads."""

from collections.abc import Mapping
from typing import Any

import pytest

from scripts.gates.intent import pr_head, pr_merged
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.ledger_events import view as read_view
from scripts.tick.deadline import Deadline
from tests.forge.fakes import API, HEADERS, Sent
from tests.forge.test_gitlab import HEAD, LINK, Refused, answer

pytestmark = pytest.mark.unit
DESCRIPTION = "Reviewer seats read the change through the forge."
MERGED_AT, MERGED_MS = "2026-11-20T10:00:00.000Z", 1_795_168_800_000


class Port:
    """A GitHub source control port that records every read and answers none of them."""

    def __init__(self) -> None:
        self.calls: list[Mapping[str, str]] = []

    def graph(self, document: str, variables: Mapping[str, str], *, deadline: Deadline) -> Any:
        """Record the read and fail it the way an unreachable GitHub does."""
        self.calls.append(variables)
        raise OSError("GitHub is not reachable")


def sent(monkeypatch: pytest.MonkeyPatch, transport: Any) -> Any:
    """Make the GitLab adapter talk through ``transport`` and return it."""
    monkeypatch.setattr("scripts.swarm.gitlab_pull.TRANSPORT", transport)
    return transport


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "fake-token")


@pytest.mark.feature("swarm-gitlab-change-close")
def test_a_gitlab_link_without_a_token_reads_no_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    assert read_view(LINK, Port()) is None


@pytest.mark.feature("swarm-gitlab-change-close")
def test_the_view_reads_an_opened_gitlab_merge_request_without_github(
    monkeypatch: pytest.MonkeyPatch, token: None
) -> None:
    port, transport = Port(), sent(monkeypatch, Sent(answer()))
    assert read_view(LINK, port) == PullRequest("OPEN", None, None, False, head=HEAD, body=DESCRIPTION)
    assert transport.calls == [("GET", f"{API}/12", HEADERS, None)]
    assert port.calls == []


@pytest.mark.feature("swarm-gitlab-change-close")
def test_the_view_reads_a_merged_gitlab_merge_request_with_its_merge_time(
    monkeypatch: pytest.MonkeyPatch, token: None
) -> None:
    sent(monkeypatch, Sent({**answer("merged", "success"), "merged_at": MERGED_AT}))
    assert read_view(LINK, Port()) == PullRequest("MERGED", MERGED_MS, None, False, head=HEAD, body=DESCRIPTION)


@pytest.mark.feature("swarm-gitlab-change-close")
def test_the_view_reads_a_closed_gitlab_merge_request_as_closed(monkeypatch: pytest.MonkeyPatch, token: None) -> None:
    sent(monkeypatch, Sent(answer("closed", "canceled")))
    pull = read_view(LINK, Port())
    assert pull is not None and (pull.state, pull.merged_at, pull.queued) == ("CLOSED", None, False)


@pytest.mark.feature("swarm-gitlab-change-close")
def test_the_view_carries_the_gitlab_draft_flag(monkeypatch: pytest.MonkeyPatch, token: None) -> None:
    sent(monkeypatch, Sent({**answer(), "draft": True}))
    assert read_view(LINK, Port()) == PullRequest("OPEN", None, None, False, head=HEAD, draft=True, body=DESCRIPTION)


@pytest.mark.feature("swarm-gitlab-change-close")
def test_a_missing_token_reads_nothing_from_gitlab_or_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    port, transport = Port(), sent(monkeypatch, Sent())
    assert read_view(LINK, port) is None
    assert transport.calls == [] and port.calls == []


@pytest.mark.feature("swarm-gitlab-change-close")
def test_a_missing_gitlab_merge_request_reads_no_pull_request(monkeypatch: pytest.MonkeyPatch, token: None) -> None:
    port, transport = Port(), sent(monkeypatch, Refused(404))
    assert read_view(LINK, port) is None
    assert transport.calls == [f"{API}/12"] and port.calls == []


@pytest.mark.feature("swarm-gitlab-change-close")
def test_a_gitlab_server_error_reads_no_pull_request(monkeypatch: pytest.MonkeyPatch, token: None) -> None:
    sent(monkeypatch, Refused(500))
    assert read_view(LINK, Port()) is None


@pytest.mark.feature("swarm-gitlab-change-close")
def test_the_intent_head_read_answers_the_gitlab_merge_request_head(
    monkeypatch: pytest.MonkeyPatch, token: None
) -> None:
    transport = sent(monkeypatch, Sent(answer()))
    assert pr_head(LINK) == HEAD
    assert transport.calls == [("GET", f"{API}/12", HEADERS, None)]


@pytest.mark.feature("swarm-gitlab-change-close")
def test_the_intent_head_read_of_a_missing_merge_request_is_none(monkeypatch: pytest.MonkeyPatch, token: None) -> None:
    sent(monkeypatch, Refused(404))
    assert pr_head(LINK) is None


@pytest.mark.feature("swarm-gitlab-change-close")
def test_the_intent_merged_read_is_true_only_for_a_merged_gitlab_merge_request(
    monkeypatch: pytest.MonkeyPatch, token: None
) -> None:
    sent(monkeypatch, Sent(answer("merged", "success"), answer(), answer("closed", "canceled")))
    assert [pr_merged(LINK), pr_merged(LINK), pr_merged(LINK)] == [True, False, False]
