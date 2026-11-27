from collections.abc import Mapping
from typing import Any

from scripts.ci_lab.webhooks import HeadEvent

PUSH = "push"


def without_lane(payload: Mapping[str, Any], repo: str, actor: str) -> HeadEvent:
    # ruleid: lab-push-head-without-lane
    return HeadEvent("push", repo, payload["after"], payload["before"], actor, "pushed")


def named_without_lane(repo: str, head: str, base: str, actor: str) -> HeadEvent:
    # ruleid: lab-push-head-without-lane
    return HeadEvent(PUSH, repo, head, base, actor, "pushed")


def keywords_without_lane(repo: str, head: str, base: str, actor: str) -> HeadEvent:
    # ruleid: lab-push-head-without-lane
    return HeadEvent(event="push", repo=repo, head=head, base=base, actor=actor, action="pushed")


def some_keywords_without_lane(repo: str, head: str, base: str, actor: str) -> HeadEvent:
    # ruleid: lab-push-head-without-lane
    return HeadEvent(PUSH, repo, head, base, actor=actor, action="pushed")


def with_lane(payload: Mapping[str, Any], repo: str, actor: str, branch: str) -> HeadEvent:
    # ok: lab-push-head-without-lane
    return HeadEvent("push", repo, payload["after"], payload["before"], actor, "pushed", branch)


def named_with_lane(repo: str, head: str, base: str, actor: str, branch: str) -> HeadEvent:
    # ok: lab-push-head-without-lane
    return HeadEvent(PUSH, repo, head, base, actor, "pushed", branch)


def keywords_with_lane(repo: str, head: str, base: str, actor: str, branch: str) -> HeadEvent:
    # ok: lab-push-head-without-lane
    return HeadEvent(event="push", repo=repo, head=head, base=base, actor=actor, action="pushed", lane=branch)


def some_keywords_with_lane(repo: str, head: str, base: str, actor: str, branch: str) -> HeadEvent:
    # ok: lab-push-head-without-lane
    return HeadEvent(PUSH, repo, head, base, actor, "pushed", lane=branch)


def another_event(repo: str, head: str, actor: str) -> HeadEvent:
    # ok: lab-push-head-without-lane
    return HeadEvent("plant", repo, head, "", actor, "pushed")
