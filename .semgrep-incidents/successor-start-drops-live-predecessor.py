from typing import Any


def start_dropping(registry: Any, slug: str, current: Any, previous: Any) -> None:
    # ruleid: successor-start-drops-live-predecessor
    removed = previous.name if previous and previous.name != current.name else ""
    registry._project(slug, (current.name,), current, removed=removed)


def start_dropping_through_store(store: Any, slug: str, current: Any, previous: Any) -> None:
    # ruleid: successor-start-drops-live-predecessor
    removed = previous.name if previous else ""
    store.project_agent(slug, current, removed=removed)


def start_dropping_directly(store: Any, slug: str, current: Any, previous: Any) -> None:
    # ruleid: successor-start-drops-live-predecessor
    store.project_agent(slug, current, removed=previous.name)


def start_keeping(registry: Any, slug: str, current: Any, previous: Any) -> None:
    # ok: successor-start-drops-live-predecessor
    removed = previous.name if previous and previous.state == "ended" and previous.name != current.name else ""
    registry._project(slug, (current.name,), current, removed=removed)


def rename(store: Any, slug: str, agent: Any, new: str) -> None:
    # ok: successor-start-drops-live-predecessor
    store.project_agent(slug, agent, removed=new)
