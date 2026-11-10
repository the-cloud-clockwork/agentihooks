from typing import Any

from scripts.swarm import controller
# ruleid: tick-fenced-runtime
from scripts.swarm.controller import FencedRuntime


def place(store: Any, slug: str, held: Any, runtime: Any) -> Any:
    # ruleid: tick-fenced-runtime
    return controller.FencedRuntime(store, slug, held, runtime, controller.LOCAL)


def respawn(store: Any, slug: str, held: Any, runtime: Any) -> Any:
    # ruleid: tick-fenced-runtime
    return FencedRuntime(store, slug, held, runtime, controller.LOCAL)


def through_the_port(tools: Any, held: Any) -> Any:
    # ok: tick-fenced-runtime
    return tools.runtime.fence(held, controller.LOCAL, deadline=None)
