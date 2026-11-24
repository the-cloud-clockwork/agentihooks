from typing import Any

from scripts.events import subjects
from scripts.events.subjects import subject


def review_on_the_routed_log(bus: Any, slug: str) -> Any:
    # ruleid: review-pass-own-swarm-events
    return bus.events.consumer(f"review-pr-{slug}", subject(slug, "pull", "opened"))


def review_on_the_routed_log_by_module(bus: Any, slug: str) -> Any:
    # ruleid: review-pass-own-swarm-events
    return bus.events.consumer(f"review-mq-{slug}", subjects.subject(slug, "merge_group", "requested"))


def review_through_a_routed_log_variable(bus: Any, slug: str) -> Any:
    log = bus.events
    # ruleid: review-pass-own-swarm-events
    return log.consumer(f"review-rel-{slug}", subject(slug, "release", "published"))


def review_on_the_swarm_log(bus: Any, slug: str) -> Any:
    # ok: review-pass-own-swarm-events
    return bus.for_swarm(slug).events.consumer(f"review-pr-{slug}", subject(slug, "pull", "opened"))


def review_through_a_swarm_log_variable(bus: Any, slug: str) -> Any:
    events = bus.for_swarm(slug).events
    # ok: review-pass-own-swarm-events
    return events.consumer(f"review-rel-{slug}", subject(slug, "release", "published"))


def review_through_a_swarm_view(bus: Any, slug: str) -> Any:
    view = bus.for_swarm(slug)
    # ok: review-pass-own-swarm-events
    return view.events.consumer(f"review-mq-{slug}", subject(slug, "merge_group", "requested"))


def reactor_on_the_routed_log(bus: Any) -> Any:
    # ok: review-pass-own-swarm-events
    return bus.events.consumer("reactor", "swarm.*.pull.opened")
