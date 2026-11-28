"""A pull request head event keeps the number of its pull request, issue 13890."""

from typing import Any

import pytest

from scripts.ci_lab import webhooks
from scripts.ci_lab.webhooks import HeadEvent

REPO = "https://github.com/acme/widgets"
HEAD = "a" * 40
BASE = "b" * 40
ACTOR = "nestorcolt"


def _pull_request(number: int = 41, pulls: bool = True) -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"html_url": REPO},
        "sender": {"login": ACTOR},
        "pull_request": {
            "number": number,
            "head": {"sha": HEAD, "ref": "feature", "label": "acme:feature"},
            "base": {"sha": BASE, "label": "acme:dev"},
            "user": {"login": ACTOR},
        },
    }


def _check_run(number: int = 41, pulls: bool = True) -> dict[str, Any]:
    return {
        "action": "rerequested",
        "repository": {"html_url": REPO},
        "sender": {"login": ACTOR},
        "check_run": {
            "name": "lab / unit",
            "head_sha": HEAD,
            "pull_requests": [{"number": number, "base": {"sha": BASE}}] if pulls else [],
        },
    }


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_pull_request_head_event_keeps_the_pull_request_number() -> None:
    [event] = webhooks.head_events("pull_request", _pull_request(41))
    assert isinstance(event, HeadEvent)
    assert event.pull_number == 41


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_rerequested_check_run_naming_a_pull_request_keeps_its_number() -> None:
    [event] = webhooks.head_events("check_run", _check_run(41))
    assert isinstance(event, HeadEvent)
    assert event.pull_number == 41


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_rerequested_check_run_naming_no_pull_request_keeps_no_number() -> None:
    [event] = webhooks.head_events("check_run", _check_run(pulls=False))
    assert isinstance(event, HeadEvent)
    assert event.pull_number == 0


def test_a_rerequested_check_run_whose_pull_entry_has_no_number_defaults_to_zero() -> None:
    payload = {
        "action": "rerequested",
        "repository": {"html_url": REPO},
        "sender": {"login": ACTOR},
        "check_run": {
            "name": "lab / unit",
            "head_sha": HEAD,
            "pull_requests": [{"base": {"sha": BASE}}],
        },
    }
    [event] = webhooks.head_events("check_run", payload)
    assert isinstance(event, HeadEvent)
    assert event.pull_number == 0


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_push_head_event_keeps_no_pull_request_number() -> None:
    payload = {
        "ref": "refs/heads/dev",
        "before": BASE,
        "after": HEAD,
        "deleted": False,
        "repository": {"html_url": REPO},
        "sender": {"login": ACTOR},
    }
    [event] = webhooks.head_events("push", payload)
    assert isinstance(event, HeadEvent)
    assert event.pull_number == 0


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_a_merge_group_head_event_keeps_no_pull_request_number() -> None:
    payload = {
        "action": "checks_requested",
        "repository": {"html_url": REPO},
        "sender": {"login": "github-merge-queue[bot]"},
        "merge_group": {"head_sha": HEAD, "base_sha": BASE, "head_ref": "refs/heads/gh-readonly-queue/dev/pr-7"},
    }
    [event] = webhooks.head_events("merge_group", payload)
    assert isinstance(event, HeadEvent)
    assert event.pull_number == 0


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_head_data_carries_the_pull_request_number_when_it_is_set() -> None:
    event = HeadEvent("pull_request", REPO, HEAD, BASE, ACTOR, "opened", "feature", 41)
    assert webhooks.head_data(event) == {
        "event": "pull_request",
        "repo": REPO,
        "head": HEAD,
        "base": BASE,
        "actor": ACTOR,
        "action": "opened",
        "lane": "feature",
        "pull_number": 41,
    }


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_head_data_omits_the_pull_request_number_when_it_is_not_set() -> None:
    event = HeadEvent("pull_request", REPO, HEAD, BASE, ACTOR, "opened", "feature")
    assert webhooks.head_data(event) == {
        "event": "pull_request",
        "repo": REPO,
        "head": HEAD,
        "base": BASE,
        "actor": ACTOR,
        "action": "opened",
        "lane": "feature",
    }


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_an_envelope_without_the_pull_request_number_keeps_the_id_a_head_without_the_field_would_have() -> None:
    with_default = HeadEvent("pull_request", REPO, HEAD, BASE, ACTOR, "opened", "feature")
    explicit_zero = HeadEvent("pull_request", REPO, HEAD, BASE, ACTOR, "opened", "feature", 0)
    assert webhooks.envelope(with_default).id == webhooks.envelope(explicit_zero).id


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_an_envelope_with_the_pull_request_number_gets_a_different_id() -> None:
    without = HeadEvent("pull_request", REPO, HEAD, BASE, ACTOR, "opened", "feature")
    with_number = HeadEvent("pull_request", REPO, HEAD, BASE, ACTOR, "opened", "feature", 41)
    assert webhooks.envelope(without).id != webhooks.envelope(with_number).id
