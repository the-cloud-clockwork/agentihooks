import sys
from typing import Any


class Unisolated:
    def end_pass(self, store: Any, slug: str, idle: Any) -> None:
        # ruleid: waits-end-without-agent-isolation
        for agent in store.agents(slug):
            idle.wait(store.redis, slug, agent.name)


class OuterCatch:
    def end_pass(self, store: Any, slug: str, idle: Any) -> None:
        try:
            # ruleid: waits-end-without-agent-isolation
            for agent in store.agents(slug):
                idle.wait(store.redis, slug, agent.name)
        except Exception as exc:
            print(exc, file=sys.stderr)


class PartialCatch:
    def end_pass(self, store: Any, slug: str, idle: Any) -> None:
        # ruleid: waits-end-without-agent-isolation
        for agent in store.agents(slug):
            held = idle.wait(store.redis, slug, agent.name)
            try:
                idle.resolve(held)
            except Exception as exc:
                print(exc, file=sys.stderr)


class Isolated:
    def end_pass(self, store: Any, slug: str, idle: Any) -> None:
        # ok: waits-end-without-agent-isolation
        for agent in store.agents(slug):
            try:
                idle.wait(store.redis, slug, agent.name)
            except Exception as exc:
                print(f"wait of {agent.name} failed: {exc}", file=sys.stderr)
