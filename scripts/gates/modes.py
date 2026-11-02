"""Each gate's mode: enforce denies, observe logs the would-be deny, off skips the gate.

Inside a swarm the mode is the swarm config's gates entry, set only by the operator or the master; AGENTIHOOKS_GATE_<NAME> picks it
only outside a swarm.
"""

import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

MODES = ("enforce", "observe", "off")


class Named(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def default_mode(self) -> str: ...


LABELS = {"enforce": "deny", "observe": "log only", "off": "skip", "coach": "coach"}
ALIASES = {label: mode for mode, label in LABELS.items()}


def supported(name: str) -> tuple[str, ...]:
    return (*MODES, "coach") if name == "intent" else MODES


def normalize(value: str) -> str:
    return ALIASES.get(value, value)


def label(value: str) -> str:
    return LABELS.get(value, value)


def env_name(gate_name: str) -> str:
    return "AGENTIHOOKS_GATE_" + re.sub(r"[^A-Z0-9]", "_", gate_name.upper())


def mode(gate: Named, environ: Mapping[str, str], gates: Mapping[str, str] | None = None) -> str:
    if gates is not None:
        return configured(gate, gates)
    chosen = str(environ.get(env_name(gate.name))).strip().lower()
    return chosen if chosen in supported(gate.name) else gate.default_mode


def configured(gate: Named, gates: Mapping[str, str]) -> str:
    chosen = str(gates.get(gate.name)).strip().lower()
    return chosen if chosen in supported(gate.name) else gate.default_mode


def swarm_gates(swarm: str, environ: Mapping[str, str]) -> Mapping[str, str] | None:
    if not swarm:
        return None
    import redis

    import scripts.swarm.store
    from scripts.swarm.store import RedisStore, SwarmError

    try:
        client = cast(Callable[[Mapping[str, str]], Any], scripts.swarm.store.redis_client)(environ)
        return cast(dict[str, str], RedisStore(client).config(swarm).gates)
    except (redis.RedisError, SwarmError, ValueError):  # an unreadable swarm config falls back to the environment
        return None
