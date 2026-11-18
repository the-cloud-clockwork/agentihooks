"""Tests for the culprit of a red merge group: naming, the comment, the App poster and the dequeued event."""

from collections.abc import Mapping
from typing import cast
from unittest.mock import Mock, call

import pytest
import requests

from scripts import github_app
from scripts.ci_lab import culprit
from scripts.ci_lab.checks import PostRefused
from scripts.ci_lab.culprit import CULPRIT, INTERACTION, Culprits, Entry, Finding
from scripts.ci_lab.store import Verdict
from scripts.ci_lab.units import Unit
from scripts.events.bus import Bus, Consumer
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.events.memory import open_bus
from scripts.hub.pulls import Observation, Pulls

pytestmark = pytest.mark.unit

GROUP = "c" * 40
FIRST = "https://github.com/example/repo/pull/7"
SECOND = "https://github.com/example/repo/pull/8"
THIRD = "https://github.com/example/repo/pull/9"
DETAILS = "https://lab.example/grades/run-7"
COMMENT = "https://github.com/example/repo/pull/8#issuecomment-11"
FIRST_COMMENT = "https://github.com/example/repo/pull/7#issuecomment-10"
GRAPHQL = "https://api.github.com/graphql"
HEADERS = {
    "Authorization": "Bearer fixture",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class Cache:
    """A verdict cache over unit keys mapped to passed, missing every other key, that records each read."""

    def __init__(self, passed: Mapping[str, bool]) -> None:
        self.passed = dict(passed)
        self.asked: list[tuple[str, str]] = []

    def get(self, kind: str, key: str) -> Verdict | None:
        """Return the verdict of a known key, else None."""
        self.asked.append((kind, key))
        known = self.passed.get(key)
        return None if known is None else Verdict(kind, key, known, False, 1.0)


class Token:
    """A token source that always hands out the fixture token."""

    def get_token(self) -> str:
        """Return the fixture token."""
        return "fixture"


def entry(url: str, *keys: str) -> Entry:
    """Return a group entry whose alone grade is the tests units of the given keys."""
    return Entry(url, tuple(Unit("tests", key, ()) for key in keys))


def answer(status: int, body: Mapping[str, object] | None = None) -> Mock:
    """Return a mocked HTTP response."""
    return Mock(status_code=status, json=Mock(return_value=dict(body or {})))


def poster(monkeypatch: pytest.MonkeyPatch, *answers: Mock) -> tuple[Culprits, Mock]:
    """Return a poster for example/repo whose requests answer in order, with its request mock."""
    request = Mock(side_effect=list(answers))
    monkeypatch.setattr(requests, "request", request)
    return Culprits("example/repo", Token()), request


def queued(url: str) -> tuple[Pulls, Consumer]:
    """Return a hub tracking the queued pull request at url for the crew swarm, and a consumer of its events."""
    bus = cast(Bus, open_bus(BusConfig()))
    hub = Pulls(bus, fetch=lambda seen: pytest.fail(f"unexpected fetch of {seen}"), clock=lambda: 1000)
    watch = bus.events.consumer("watch", "swarm.>")
    hub.handle(Envelope("task-crew-1", "task.changed", "crew", "task", "t1", 1, 900, "ledger", {"pr_url": url}))
    hub.observe(Observation(url, "open", "a1", True))
    return hub, watch


@pytest.mark.feature("lab-red-group-culprit")
def test_the_first_entry_red_alone_is_the_culprit() -> None:
    cache = Cache({"one": True, "two": False, "three": False})
    entries = (entry(FIRST, "one"), entry(SECOND, "two"), entry(THIRD, "three"))
    found = culprit.name(cache, GROUP, entries)
    assert found == Finding(CULPRIT, GROUP, (SECOND,))
    assert found is not None
    assert found.removed == SECOND
    assert cache.asked == [("tests", "one"), ("tests", "two")]


@pytest.mark.feature("lab-red-group-culprit")
def test_an_entry_is_red_alone_when_any_unit_fails_while_another_misses_the_cache() -> None:
    cache = Cache({"two": False, "three": True})
    found = culprit.name(cache, GROUP, (entry(FIRST, "one", "two", "three"),))
    assert found == Finding(CULPRIT, GROUP, (FIRST,))


@pytest.mark.feature("lab-red-group-culprit")
def test_a_red_group_green_entry_by_entry_is_an_interaction_naming_both() -> None:
    cache = Cache({"one": True, "two": True, "three": True})
    found = culprit.name(cache, GROUP, (entry(FIRST, "one"), entry(SECOND, "two", "three")))
    assert found == Finding(INTERACTION, GROUP, (FIRST, SECOND))
    assert found is not None
    assert found.removed == SECOND


@pytest.mark.feature("lab-red-group-culprit")
def test_an_entry_with_no_units_to_grade_is_green_alone() -> None:
    found = culprit.name(Cache({"two": True}), GROUP, (entry(FIRST), entry(SECOND, "two")))
    assert found == Finding(INTERACTION, GROUP, (FIRST, SECOND))


@pytest.mark.feature("lab-red-group-culprit")
def test_a_miss_ahead_of_the_first_red_entry_leaves_the_group_undecided() -> None:
    assert culprit.name(Cache({"two": False}), GROUP, (entry(FIRST, "one"), entry(SECOND, "two"))) is None
    assert culprit.name(Cache({"one": True}), GROUP, (entry(FIRST, "one"), entry(SECOND, "two"))) is None


@pytest.mark.feature("lab-red-group-culprit")
def test_a_miss_behind_the_culprit_does_not_hold_the_finding() -> None:
    found = culprit.name(Cache({"one": False}), GROUP, (entry(FIRST, "one"), entry(SECOND, "two")))
    assert found == Finding(CULPRIT, GROUP, (FIRST,))


@pytest.mark.feature("lab-red-group-culprit")
def test_a_group_of_fewer_than_two_entries_green_alone_names_nothing() -> None:
    assert culprit.name(Cache({}), GROUP, ()) is None
    assert culprit.name(Cache({"one": True}), GROUP, (entry(FIRST, "one"),)) is None


@pytest.mark.feature("lab-red-group-culprit")
def test_the_culprit_comment_names_the_group_and_that_it_leaves_the_queue() -> None:
    assert culprit.body(Finding(CULPRIT, GROUP, (SECOND,)), DETAILS) == (
        f"The lab graded the merge group at {GROUP} red. Graded alone against dev from the lab's cache, "
        f"{SECOND} is red, so it is the culprit. The lab is removing {SECOND} from the merge queue so the queue "
        f"re-forms without it.\n\nDetails: {DETAILS}"
    )


@pytest.mark.feature("lab-red-group-culprit")
def test_the_interaction_comment_names_every_entry_and_the_one_that_leaves() -> None:
    assert culprit.body(Finding(INTERACTION, GROUP, (FIRST, SECOND, THIRD)), DETAILS) == (
        f"The lab graded the merge group at {GROUP} red. Every entry is green alone against dev from the lab's "
        f"cache, so the group is red only together: an interaction of {FIRST}, {SECOND}, {THIRD}. "
        f"The lab is removing {THIRD} from the merge queue so the queue re-forms without it.\n\nDetails: {DETAILS}"
    )


@pytest.mark.feature("lab-red-group-culprit")
def test_the_culprit_token_carries_only_pull_requests_and_contents_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    monkeypatch.setattr(github_app.jwt, "encode", Mock(return_value="signed"))
    granted = {
        "token": "lab-token",
        "expires_at": "2099-01-01T00:00:00Z",
        "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        "repositories": [{"full_name": "example/repo"}],
    }
    mint = Mock(return_value=answer(201, granted))
    monkeypatch.setattr(requests, "post", mint)
    request = Mock(return_value=answer(201, {"html_url": COMMENT}))
    monkeypatch.setattr(requests, "request", request)
    assert Culprits("example/repo").comment(SECOND, "text") == COMMENT
    permissions = {"contents": "write", "pull_requests": "write"}
    assert mint.call_args.kwargs["json"] == {"repositories": ["repo"], "permissions": permissions}
    assert request.call_args.kwargs["headers"]["Authorization"] == "Bearer lab-token"


@pytest.mark.feature("lab-red-group-culprit")
def test_comment_posts_the_body_on_the_pull_request_and_returns_its_url(monkeypatch: pytest.MonkeyPatch) -> None:
    lab, request = poster(monkeypatch, answer(201, {"html_url": COMMENT}))
    assert lab.comment(SECOND, "text") == COMMENT
    assert request.call_args_list == [
        call(
            "POST",
            "https://api.github.com/repos/example/repo/issues/8/comments",
            headers=HEADERS,
            json={"body": "text"},
            timeout=30,
        )
    ]


@pytest.mark.feature("lab-red-group-culprit")
def test_a_refused_comment_raises_naming_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _ = poster(monkeypatch, answer(200, {"html_url": COMMENT}))
    with pytest.raises(PostRefused, match="^POST repos/example/repo/issues/8/comments answered 200$"):
        lab.comment(SECOND, "text")


@pytest.mark.feature("lab-red-group-culprit")
def test_dequeue_removes_the_pull_request_by_its_node_id(monkeypatch: pytest.MonkeyPatch) -> None:
    node = answer(200, {"data": {"resource": {"id": "PR_node"}}})
    removed = answer(200, {"data": {"dequeuePullRequest": {"mergeQueueEntry": {"id": "MQE_entry"}}}})
    lab, request = poster(monkeypatch, node, removed)
    lab.dequeue(SECOND)
    sent = {"headers": HEADERS, "timeout": 30}
    assert request.call_args_list == [
        call("POST", GRAPHQL, json={"query": culprit.NODE, "variables": {"url": SECOND}}, **sent),
        call("POST", GRAPHQL, json={"query": culprit.DEQUEUE, "variables": {"id": "PR_node"}}, **sent),
    ]
    assert culprit.NODE == "query($url: URI!) { resource(url: $url) { ... on PullRequest { id } } }"
    assert culprit.DEQUEUE == "mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { mergeQueueEntry { id } } }"


@pytest.mark.feature("lab-red-group-culprit")
def test_a_graphql_error_refuses_the_dequeue(monkeypatch: pytest.MonkeyPatch) -> None:
    node = answer(200, {"data": {"resource": {"id": "PR_node"}}})
    errors = [{"message": "not queued"}, {"message": "stale head"}]
    lab, _ = poster(monkeypatch, node, answer(200, {"errors": errors, "data": None}))
    with pytest.raises(PostRefused, match="^POST graphql answered errors: not queued; stale head$"):
        lab.dequeue(SECOND)


@pytest.mark.feature("lab-red-group-culprit")
@pytest.mark.parametrize("resource", [None, {}], ids=["no-resource", "not-a-pull-request"])
def test_a_url_that_names_no_pull_request_refuses_the_dequeue(
    monkeypatch: pytest.MonkeyPatch, resource: dict[str, str] | None
) -> None:
    lab, request = poster(monkeypatch, answer(200, {"data": {"resource": resource}}))
    with pytest.raises(PostRefused, match=f"^GitHub has no pull request at {SECOND}$"):
        lab.dequeue(SECOND)
    assert request.call_count == 1


@pytest.mark.feature("lab-red-group-culprit")
def test_a_refused_graphql_answer_raises_naming_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    lab, _ = poster(monkeypatch, answer(502))
    with pytest.raises(PostRefused, match="^POST graphql answered 502$"):
        lab.dequeue(SECOND)


@pytest.mark.feature("lab-red-group-culprit")
def test_the_dequeue_event_of_the_removed_entry_carries_its_culprit() -> None:
    hub, watch = queued(SECOND)
    found = Finding(INTERACTION, GROUP, (FIRST, SECOND))
    hub.observe(culprit.observation(found, (COMMENT,)))
    hub.observe(Observation(SECOND, None, None, False))
    events = [delivery.event for delivery in watch.fetch(100, 0)]
    assert [event.type for event in events] == ["pull.opened", "merge.queued", "merge.dequeued"]
    named = {"kind": INTERACTION, "group": GROUP, "urls": [FIRST, SECOND], "comments": [COMMENT]}
    assert events[-1].data == {"url": SECOND, "state": "open", "head": "a1", "queued": False, "culprit": named}


@pytest.mark.feature("lab-red-group-culprit")
def test_a_dequeue_seen_on_github_carries_no_culprit() -> None:
    hub, watch = queued(SECOND)
    hub.observe(Observation(SECOND, None, None, False))
    events = [delivery.event for delivery in watch.fetch(100, 0)]
    assert events[-1].type == "merge.dequeued"
    assert events[-1].data == {"url": SECOND, "state": "open", "head": "a1", "queued": False}


@pytest.mark.feature("lab-red-group-culprit")
def test_remove_comments_on_each_named_entry_then_announces_and_dequeues_the_removed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub, watch = queued(SECOND)
    first, second = answer(201, {"html_url": FIRST_COMMENT}), answer(201, {"html_url": COMMENT})
    node = answer(200, {"data": {"resource": {"id": "PR_node"}}})
    removed = answer(200, {"data": {"dequeuePullRequest": {"mergeQueueEntry": None}}})
    lab, request = poster(monkeypatch, first, second, node, removed)
    found = Finding(INTERACTION, GROUP, (FIRST, SECOND))
    assert culprit.remove(found, DETAILS, lab, hub) == (FIRST_COMMENT, COMMENT)
    text = {"body": culprit.body(found, DETAILS)}
    assert [sent.args[1] for sent in request.call_args_list] == [
        "https://api.github.com/repos/example/repo/issues/7/comments",
        "https://api.github.com/repos/example/repo/issues/8/comments",
        GRAPHQL,
        GRAPHQL,
    ]
    assert [sent.kwargs["json"] for sent in request.call_args_list][:2] == [text, text]
    assert request.call_args_list[2].kwargs["json"]["variables"] == {"url": SECOND}
    assert request.call_args_list[3].kwargs["json"]["variables"] == {"id": "PR_node"}
    hub.observe(Observation(SECOND, None, None, False))
    events = [delivery.event for delivery in watch.fetch(100, 0)]
    assert [event.type for event in events] == ["pull.opened", "merge.queued", "merge.dequeued"]
    named = {"kind": INTERACTION, "group": GROUP, "urls": [FIRST, SECOND], "comments": [FIRST_COMMENT, COMMENT]}
    assert events[-1].data["culprit"] == named


@pytest.mark.feature("lab-red-group-culprit")
def test_remove_announces_the_culprit_before_the_dequeue_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    hub, watch = queued(SECOND)
    lab, _ = poster(monkeypatch, answer(201, {"html_url": COMMENT}), answer(502))
    with pytest.raises(PostRefused, match="^POST graphql answered 502$"):
        culprit.remove(Finding(CULPRIT, GROUP, (SECOND,)), DETAILS, lab, hub)
    events = [delivery.event for delivery in watch.fetch(100, 0)]
    assert events[-1].type == "merge.dequeued"
    assert events[-1].data["culprit"] == {"kind": CULPRIT, "group": GROUP, "urls": [SECOND], "comments": [COMMENT]}
