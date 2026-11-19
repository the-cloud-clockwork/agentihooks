"""Lab head events: GitHub webhook deliveries and the signed head notice turned into ci.lab.head envelopes.

`head_events` reads the deliveries that move a head the lab may grade, `notice_event` reads the
notice the agents' push path sends to `/lab/head`, and `LabIntake` publishes each head as an
envelope whose type is its bus subject, `ci.lab.head.<event>`.
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from scripts.events.envelope import Envelope

HEAD_SUBJECT = "ci.lab.head"
LAB = "ci-lab"
ENTITY = "head"
NOTICE = "notice"
PLANT = "plant"
PUSHED = "pushed"
CHECK_PREFIX = "lab"
BRANCHES = "refs/heads/"
PLANT_PREFIX = "refs/heads/diffcheck/lab-plant-"
PULL_ACTIONS = frozenset({"opened", "synchronize", "reopened"})
NOTICE_FIELDS = ("repo", "head", "base", "actor")
NOTICE_SECRET_ENV = "AGENTIHOOKS_CI_LAB_NOTICE_SECRET"
NO_NOTICE_SECRET = f"{NOTICE_SECRET_ENV} must be set to verify lab head notices"


@dataclass(frozen=True)
class HeadEvent:
    """One head the lab may grade: the event and action that named it, its repository, head and base, and the actor."""

    event: str
    repo: str
    head: str
    base: str
    actor: str
    action: str


def _push(payload: Mapping[str, Any]) -> list[HeadEvent]:
    if payload["deleted"] or not str(payload["ref"]).startswith(BRANCHES):
        return []
    repo, actor = payload["repository"]["html_url"], payload["sender"]["login"]
    if str(payload["ref"]).startswith(PLANT_PREFIX):
        return [HeadEvent(PLANT, repo, payload["after"], "", actor, PUSHED)]
    if payload["before"] == "0" * 40:
        return []
    return [HeadEvent("push", repo, payload["after"], payload["before"], actor, PUSHED)]


def _pull_request(payload: Mapping[str, Any]) -> list[HeadEvent]:
    if payload["action"] not in PULL_ACTIONS:
        return []
    pull, repo = payload["pull_request"], payload["repository"]["html_url"]
    head, base, author = pull["head"]["sha"], pull["base"]["sha"], pull["user"]["login"]
    return [HeadEvent("pull_request", repo, head, base, author, payload["action"])]


def _merge_group(payload: Mapping[str, Any]) -> list[HeadEvent]:
    if payload["action"] != "checks_requested":
        return []
    group, repo, actor = payload["merge_group"], payload["repository"]["html_url"], payload["sender"]["login"]
    return [HeadEvent("merge_group", repo, group["head_sha"], group["base_sha"], actor, payload["action"])]


def _check_run(payload: Mapping[str, Any]) -> list[HeadEvent]:
    run = payload["check_run"]
    if payload["action"] != "rerequested" or not str(run["name"]).startswith(CHECK_PREFIX):
        return []
    pulls = run["pull_requests"]
    base = pulls[0]["base"]["sha"] if pulls else ""
    repo, actor = payload["repository"]["html_url"], payload["sender"]["login"]
    return [HeadEvent("check_run", repo, run["head_sha"], base, actor, payload["action"])]


HEADS: Mapping[str, Callable[[Mapping[str, Any]], list[HeadEvent]]] = {
    "push": _push,
    "pull_request": _pull_request,
    "merge_group": _merge_group,
    "check_run": _check_run,
}


def head_events(name: str | None, payload: Mapping[str, Any]) -> list[HeadEvent]:
    """Return the heads a GitHub delivery names for the lab.

    A pull request head's actor is the pull request's author, as `reconcile` reads it, so a trusted
    sender reopening an untrusted author's pull request admits nothing; every other head's actor is
    the delivery's sender. A rerequested check run with no pull request has no base. A push to a
    branch under `PLANT_PREFIX` names a plant head with no base, whatever its before, which the
    scheduler bases on the head's merge base with dev.
    """
    if name is None or name not in HEADS:
        return []
    return HEADS[name](payload)


def notice_event(payload: Mapping[str, Any]) -> HeadEvent:
    """Return the head a signed head notice names, refusing a notice without non empty repo, head, base and actor."""
    values = [payload[field] for field in NOTICE_FIELDS]
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"a lab head notice needs non empty {', '.join(NOTICE_FIELDS)}")
    repo, head, base, actor = values
    return HeadEvent(NOTICE, repo, head, base, actor, PUSHED)


def envelope(event: HeadEvent) -> Envelope:
    """Return the envelope recording a head, typed with its bus subject; one head event always has one id, so a resend is dropped."""
    return Envelope(
        id=hashlib.sha256(json.dumps(asdict(event)).encode()).hexdigest(),
        type=f"{HEAD_SUBJECT}.{event.event}",
        swarm=LAB,
        entity=ENTITY,
        key=f"{event.repo}@{event.head}",
        version=0,
        time=time.time_ns() // 1_000_000,
        author=event.actor,
        data=asdict(event),
    )


class Publisher(Protocol):
    """The one bus log operation the lab intake needs."""

    def publish(self, subject: str, event: Envelope) -> bool: ...


class LabIntake:
    """Publishes lab head envelopes on the bus and holds the secret that signs head notices."""

    def __init__(self, events: Publisher, secret: bytes) -> None:
        if not secret:
            raise ValueError(NO_NOTICE_SECRET)
        self.events = events
        self.secret = secret

    def publish(self, envelopes: Sequence[Envelope]) -> None:
        """Publish each envelope on the subject its type names."""
        for event in envelopes:
            self.events.publish(event.type, event)
