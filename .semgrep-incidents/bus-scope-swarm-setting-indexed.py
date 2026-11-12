import os
from collections.abc import Mapping
from typing import Any


def scoped_state(bus: Any, bucket: str) -> Any:
    # ruleid: bus-scope-swarm-setting-indexed
    return bus.for_swarm(os.environ["AGENTIHOOKS_SWARM"]).state(bucket)


def scoped_from(bus: Any, environ: Mapping[str, str], bucket: str) -> Any:
    # ruleid: bus-scope-swarm-setting-indexed
    return bus.for_swarm(environ["AGENTIHOOKS_SWARM"]).state(bucket)


def defined_scope(bus: Any, bucket: str) -> Any:
    # ok: bus-scope-swarm-setting-indexed
    swarm = os.environ.get("AGENTIHOOKS_SWARM")
    return (bus.for_swarm(swarm) if swarm else bus).state(bucket)
