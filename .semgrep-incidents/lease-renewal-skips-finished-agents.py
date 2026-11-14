from typing import Any


def renew_all(store: Any, slug: str, lease_ms: int) -> None:
    for agent in store.agents(slug):
        # ruleid: lease-renewal-skips-finished-agents
        store.refresh(slug, agent.task, agent.name, lease_ms)


def renew_outside(store: Any, slug: str, judged: set[str], lease_ms: int) -> None:
    for agent in store.agents(slug):
        if agent.name not in judged:
            # ruleid: lease-renewal-skips-finished-agents
            store.refresh(slug, agent.task, agent.name, lease_ms)


def renew_working(store: Any, slug: str, judged: set[str], lease_ms: int) -> None:
    for agent in store.agents(slug):
        if agent.name not in judged and agent.state != "finished":
            # ok: lease-renewal-skips-finished-agents
            store.refresh(slug, agent.task, agent.name, lease_ms)


def renew_skipping(store: Any, slug: str, lease_ms: int) -> None:
    for agent in store.agents(slug):
        if agent.state == "finished":
            continue
        # ok: lease-renewal-skips-finished-agents
        store.refresh(slug, agent.task, agent.name, lease_ms)
