"""Each gate's mode: enforce denies, observe logs the would-be deny, off skips the gate.

Inside a swarm the mode is the swarm config's gates entry, set only by the operator or the master; AGENTIHOOKS_GATE_<NAME> picks it
only outside a swarm.
"""

import re

MODES = ("enforce", "observe", "off")
LABELS = {"enforce": "deny", "observe": "log only", "off": "skip", "coach": "coach"}
ALIASES = {label: mode for mode, label in LABELS.items()}


def supported(name: str) -> tuple[str, ...]:
    return (*MODES, "coach") if name == "intent" else MODES


def normalize(value: str) -> str:
    return ALIASES.get(value, value)


def label(value: str) -> str:
    return LABELS.get(value, value)


def env_name(gate_name):
    return "AGENTIHOOKS_GATE_" + re.sub(r"[^A-Z0-9]", "_", gate_name.upper())


def mode(gate, environ, gates=None):
    if gates is not None:
        return configured(gate, gates)
    chosen = str(environ.get(env_name(gate.name))).strip().lower()
    return chosen if chosen in supported(gate.name) else gate.default_mode


def configured(gate, gates):
    chosen = str(gates.get(gate.name)).strip().lower()
    return chosen if chosen in supported(gate.name) else gate.default_mode


def swarm_gates(swarm, environ):
    if not swarm:
        return None
    import redis

    from scripts.swarm.store import RedisStore, SwarmError, redis_client

    try:
        return RedisStore(redis_client(environ)).config(swarm).gates
    except (redis.RedisError, SwarmError, ValueError):  # an unreadable swarm config falls back to the environment
        return None
