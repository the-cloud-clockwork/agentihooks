from collections.abc import Mapping
from typing import Any

from scripts.ci_lab.webhooks import HeadEvent, pull_lane

PULL_REQUEST = "pull_request"


def without_lane(pull: Mapping[str, Any], repo: str, action: str) -> HeadEvent:
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    # ruleid: lab-pull-request-head-without-lane
    return HeadEvent("pull_request", repo, head, base, author, action)


def named_without_lane(pull: Mapping[str, Any], repo: str) -> HeadEvent:
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    # ruleid: lab-pull-request-head-without-lane
    return HeadEvent(PULL_REQUEST, repo, head, base, author, "reconcile")


def keywords_without_lane(pull: Mapping[str, Any], repo: str, action: str) -> HeadEvent:
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    # ruleid: lab-pull-request-head-without-lane
    return HeadEvent(event="pull_request", repo=repo, head=head, base=base, actor=author, action=action)


def some_keywords_without_lane(pull: Mapping[str, Any], repo: str) -> HeadEvent:
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    # ruleid: lab-pull-request-head-without-lane
    return HeadEvent(PULL_REQUEST, repo, head, base, actor=author, action="reconcile")


def keywords_with_lane(pull: Mapping[str, Any], repo: str, action: str) -> HeadEvent:
    head, base, author, lane = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"], pull_lane(pull)
    # ok: lab-pull-request-head-without-lane
    return HeadEvent(event="pull_request", repo=repo, head=head, base=base, actor=author, action=action, lane=lane)


def some_keywords_with_lane(pull: Mapping[str, Any], repo: str) -> HeadEvent:
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    # ok: lab-pull-request-head-without-lane
    return HeadEvent(PULL_REQUEST, repo, head, base, author, "reconcile", lane=pull_lane(pull))


def with_lane(pull: Mapping[str, Any], repo: str, action: str) -> HeadEvent:
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    # ok: lab-pull-request-head-without-lane
    return HeadEvent("pull_request", repo, head, base, author, action, pull_lane(pull))


def named_with_lane(pull: Mapping[str, Any], repo: str) -> HeadEvent:
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    # ok: lab-pull-request-head-without-lane
    return HeadEvent(PULL_REQUEST, repo, head, base, author, "reconcile", pull_lane(pull))


def another_event(repo: str, head: str, actor: str) -> HeadEvent:
    # ok: lab-pull-request-head-without-lane
    return HeadEvent("plant", repo, head, "", actor, "pushed")
