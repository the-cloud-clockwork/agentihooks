"""The review steps of the tick: queue one review lane task per pull request, merge queue group and release a swarm
sees on the events bus, keyed by the change so a repeated event adds nothing."""

import hashlib
import os
import sys
from collections.abc import Callable
from functools import cache
from typing import Any, cast

from scripts.events.bus import Bus, Delivery
from scripts.events.config import DEFAULT_BACKEND, BusConfig
from scripts.events.factory import open_bus
from scripts.events.subjects import subject
from scripts.review import record
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import SENDER
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger import ledger_comments
from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, hook_domains
from scripts.tick.steps.host import held

ACTIVE = ("running", "drained")
BATCH = 50
FETCH_MS = 100
REPLAY_MS = 6 * 3_600_000
KINDS = {"pull.opened": "pr", "merge_group.requested": "mq", "release.published": "rel"}
NOUNS = {"pr": "pull request", "mq": "merge queue group", "rel": "release"}


@cache
def _bus() -> Bus | None:
    """Return the shared events bus the tick reads, or None on the in process memory backend that sees no hub."""
    config = BusConfig.from_environ(os.environ)
    return None if config.backend == DEFAULT_BACKEND else open_bus(config)


def _durable(slug: str, short: str) -> str:
    """Return the durable consumer name of swarm ``slug`` for review events of kind ``short``."""
    return f"review-{short}-{hashlib.sha256(slug.encode()).hexdigest()[:16]}"


def _change(short: str, data: dict[str, Any]) -> tuple[str, str, str]:
    """Return the identity, forge link and pull request link of the change a review event of kind ``short`` names;
    the pull request link is empty when the change names none."""
    if short == "pr":
        return data["url"], data["url"], data["url"]
    repository = data["repository"]
    if short == "mq":
        number = data.get("number")
        pull = f"{repository}/pull/{number}" if number else ""
        return f"{repository}@{data['head']}", f"{repository}/commit/{data['head']}", pull
    return f"{repository}@{data['tag']}", data["url"], ""


def _label(short: str, data: dict[str, Any], pull: str) -> str:
    """Return the plain words naming the change of a review event of kind ``short``: its pull request number, the
    pull request its merge queue group tests, or its release tag, with the repository name."""
    repository = (pull or data["repository"]).split("/")[4]
    number = pull.split("/")[-1]
    if short == "pr":
        return f"pull request {number} of {repository}"
    if short == "mq":
        tested = f" for pull request {number}" if pull else ""
        return f"the merge queue group{tested} of {repository}"
    return f"release {data['tag']} of {repository}"


def review_task(short: str, data: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """Return the review lane task for the change a review event of kind ``short`` names, with the phase of the
    author task whose pull request it carries."""
    ident, link, pull = _change(short, data)
    author = next((t for t in doc["tasks"] if pull and str(t.get("pr_url")).lower() == pull.lower()), None)
    noun = NOUNS[short]
    named = f"Review {_label(short, data, pull)}"
    by = f" Author task {author['id']}." if author else " No swarm task carries this change."
    return {
        "task": f"review-{short}-{hashlib.sha256(ident.lower().encode()).hexdigest()[:12]}",
        "title": f"Review this {noun}" if ledger_comments.problems(named, "item") else named,
        "lane": "review",
        "kind": "review",
        "phase": author.get("phase", "") if author else "",
        "description": f"Review the {noun} {link}.{by}",
    }


def _remember(store: RedisStore, slug: str, short: str, data: dict[str, Any]) -> None:
    """Record a merge queue group in the review store so its reviewers can post on the pull requests it tests."""
    if short == "mq":
        record.remember_group(
            store, slug, record.Group(data["repository"], data["head"], data["base"], data["number"] or 0)
        )


def _queue(delivery: Delivery, short: str, slug: str, doc: dict[str, Any], ledger: Any, known: set[str]) -> list[str]:
    """Add the review task of one delivery unless the ledger holds its id, returning the action it took."""
    task = review_task(short, cast(dict[str, Any], delivery.event.data), doc)
    if task["task"] in known:
        return []
    known.add(task["task"])
    try:
        ledger.add_task(slug, task, SENDER)
    except LedgerRefused as exc:
        print(f"review pass: the ledger refused {task['task']}: {exc}", file=sys.stderr)
        return []
    return [f"queued {task['task']} to review a {NOUNS[short]}"]


def review_pass(bus: Bus, slug: str, doc: dict[str, Any], ledger: Any, now_ms: int, store: RedisStore) -> list[str]:
    """Queue one review task for each pull request opened, merge queue group and release swarm ``slug`` sees on
    ``bus``, skipping every task id the ledger already holds and every event older than the replay window, and
    record each merge queue group it sees in ``store``."""
    known = {str(t["id"]) for t in doc["tasks"]}
    actions: list[str] = []
    for kind, short in KINDS.items():
        consumer = bus.for_swarm(slug).events.consumer(_durable(slug, short), subject(slug, *kind.split(".")))
        for delivery in consumer.fetch(BATCH, FETCH_MS):
            if delivery.event.time >= now_ms - REPLAY_MS:
                _remember(store, slug, short, cast(dict[str, Any], delivery.event.data))
                actions += _queue(delivery, short, slug, doc, ledger, known)
            delivery.ack()
    return actions


def _review_pass(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the review pass step, given the tick module and a reader of the pass state; the step
    is inert unless the swarm runs with a review lane cap above zero on a shared bus."""
    bus = _bus()
    if bus is None:
        return []
    config = get("store").config(get("slug"))
    if config.state not in ACTIVE or config.max_review <= 0:
        return []
    return review_pass(bus, get("slug"), get("doc"), get("ledger"), tick.now_ms(), get("store"))


registry.register(
    CallStep(
        "review pass",
        "wrapper phases",
        held(_review_pass),
        needs=frozenset(),
        reads=hook_domains("doc"),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"ledger"}),
        feature="swarm-review-trigger",
    )
)
