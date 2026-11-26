"""Grade requests from lab head events: the request builder, the trusted actor filter and open pull request reconciliation.

Every head is recorded on the bus; only a head whose actor is on the runner trust list
(`.github/workflows/trusted-actors.json`, read through `validate_actors`) becomes a grade request.
"""

import json
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.ci_lab.units import Priority, priority_of
from scripts.ci_lab.webhooks import NOTICE, PLANT, HeadEvent, pull_lane
from scripts.ci_runner_trust import ACTORS, validate_actors

__all__ = [
    "GradeRequest",
    "admit",
    "answered",
    "reconcile",
    "request_from",
    "served",
    "serves",
    "slug",
    "trusted_actors",
]

RERUN = "rerequested"
RECONCILE = "reconcile"
PULL_REQUEST = "pull_request"
TIER_EVENTS = {"check_run": PULL_REQUEST, NOTICE: PULL_REQUEST, PLANT: PULL_REQUEST}


@dataclass(frozen=True)
class GradeRequest:
    """One head of one repository the lab is asked to grade against its base, in a tier, for an actor."""

    repo: str
    head: str
    base: str
    tier: Priority
    actor: str
    bypass_negative: bool
    plant: bool = False


def request_from(event: HeadEvent) -> GradeRequest:
    """Return the grade request a head event asks for; a rerequested check bypasses a cached negative verdict."""
    return GradeRequest(
        repo=event.repo,
        head=event.head,
        base=event.base,
        tier=priority_of({"event_name": TIER_EVENTS.get(event.event, event.event)}),
        actor=event.actor,
        bypass_negative=event.action == RERUN,
        plant=event.event == "plant",
    )


def trusted_actors(root: Path) -> frozenset[str]:
    """Return the runner trust list of a tree: its approved logins and its merge queue identity."""
    return frozenset(validate_actors(json.loads((root / ACTORS).read_text())))


def admit(event: HeadEvent, trusted: Collection[str]) -> GradeRequest | None:
    """Return the grade request for a trusted actor's head, and none for an untrusted head, which stays recorded only."""
    return request_from(event) if event.actor in trusted else None


def slug(repo: str) -> str:
    """Return the owner and name of a repository URL, case folded: its path with no slash at either end and no
    trailing `.git`."""
    return repo


def served(listed: str, repository: str) -> frozenset[str]:
    """Return the slugs of the repositories a scheduler serves: those a list of URLs separated by commas or white
    space names, and when it names none, the one repository the service is set up for."""
    del listed, repository
    return frozenset()


def serves(repositories: Collection[str] | None, repo: str) -> bool:
    """Whether a head of the repository is served; a scheduler with no set of served repositories serves every one."""
    del repositories, repo
    return True


def answered(request: GradeRequest, negative: Callable[[str, str], bool]) -> bool:
    """Return whether a cached negative verdict, `negative(repo, head)`, already answers the request; a bypass never is."""
    return not request.bypass_negative and negative(request.repo, request.head)


def reconcile(open_pull_requests: Iterable[Mapping[str, Any]], graded: Callable[[str, str], bool]) -> list[HeadEvent]:
    """Return the heads of the open pull requests, as GitHub lists them, that `graded(repo, head)` says have no grade."""
    heads: list[HeadEvent] = []
    for pull in open_pull_requests:
        repo, head = pull["base"]["repo"]["html_url"], pull["head"]["sha"]
        if not graded(repo, head):
            author, lane = pull["user"]["login"], pull_lane(pull)
            heads.append(HeadEvent(PULL_REQUEST, repo, head, pull["base"]["sha"], author, RECONCILE, lane))
    return heads
