"""Each gate's mode: enforce denies, observe logs the would-be deny, off skips the gate.

Inside a swarm the mode is the swarm config's gates entry, set only by the operator; AGENTIHOOKS_GATE_<NAME> picks it
only outside a swarm.
"""

import re

MODES = ("enforce", "observe", "off")


def env_name(gate_name):
    return "AGENTIHOOKS_GATE_" + re.sub(r"[^A-Z0-9]", "_", gate_name.upper())


def mode(gate, environ, gates=None):
    chosen = environ.get(env_name(gate.name)) if gates is None else gates.get(gate.name)
    chosen = str(chosen).strip().lower()
    return chosen if chosen in MODES else gate.default_mode


def swarm_gates(swarm, environ):
    if not swarm:
        return None
    import redis

    from scripts.swarm.store import RedisStore, SwarmError, redis_client

    try:
        return RedisStore(redis_client(environ)).config(swarm).gates
    except (redis.RedisError, SwarmError, ValueError):  # an unreadable swarm config falls back to the environment
        return None
