import sys
from typing import Any


def end_pass_unisolated(store: Any, slug: str, idle: Any) -> None:
    # ruleid: waits-end-without-agent-isolation
    for agent in store.agents(slug):
        idle.wait(store.redis, slug, agent.name)


def end_pass_outer_catch(store: Any, slug: str, idle: Any) -> None:
    try:
        # ruleid: waits-end-without-agent-isolation
        for agent in store.agents(slug):
            idle.wait(store.redis, slug, agent.name)
    except Exception as exc:
        print(exc, file=sys.stderr)


def end_pass_partial_catch(store: Any, slug: str, idle: Any) -> None:
    # ruleid: waits-end-without-agent-isolation
    for agent in store.agents(slug):
        held = idle.wait(store.redis, slug, agent.name)
        try:
            idle.resolve(held)
        except Exception as exc:
            print(exc, file=sys.stderr)


def end_pass_isolated(store: Any, slug: str, idle: Any) -> None:
    # ok: waits-end-without-agent-isolation
    for agent in store.agents(slug):
        try:
            idle.wait(store.redis, slug, agent.name)
        except Exception as exc:
            print(f"wait of {agent.name} failed: {exc}", file=sys.stderr)


def enforce_pass_unisolated(store: Any, slug: str, idle: Any) -> None:
    # ruleid: waits-end-without-agent-isolation
    for agent in store.agents(slug):
        idle.wait(store.redis, slug, agent.name)


def enforce_pass_isolated(store: Any, slug: str, idle: Any) -> None:
    # ok: waits-end-without-agent-isolation
    for agent in store.agents(slug):
        try:
            idle.wait(store.redis, slug, agent.name)
        except Exception as exc:
            print(f"push wait of {agent.name} failed: {exc}", file=sys.stderr)
