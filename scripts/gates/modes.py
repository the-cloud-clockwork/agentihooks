"""Each gate's mode: enforce denies, observe logs the would-be deny, off skips the gate.

Inside a swarm the mode is the swarm config's gates entry, set only by the operator or the master; AGENTIHOOKS_GATE_<NAME> picks it
only outside a swarm.
"""

import json
import os
import re
import time
from collections.abc import Mapping, MutableMapping
from pathlib import Path

CACHE_S = 5.0
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


def swarm_gates(swarm: str, environ: MutableMapping[str, str]) -> dict[str, str] | None:
    """Return the swarm config's gates entry, read through the hook Redis client and kept on disk for CACHE_S."""
    if not swarm:
        return None
    path = _cache_path(swarm, environ)
    held = _held(path)
    if held is not None and 0 <= time.time() - held[0] < CACHE_S:
        return held[1]
    gates = _read_gates(swarm, environ)
    _keep(path, gates)
    return gates


def _held(path: Path) -> tuple[float, dict[str, str] | None] | None:
    """Return the cached read time and gates entry, or None when the cache file is missing or unreadable."""
    try:
        raw = json.loads(path.read_text())
        return float(raw["at"]), raw["gates"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _keep(path: Path, gates: dict[str, str] | None) -> bool:
    """Write the gates entry for the next gate shims; a write that fails only loses the cache."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.{os.getpid()}")
        staged.write_text(json.dumps({"at": time.time(), "gates": gates}))
        staged.replace(path)
        return True
    except OSError:
        return False


def _cache_path(swarm: str, environ: Mapping[str, str]) -> Path:
    """Return the file that holds the swarm's gates entry between gate shim processes."""
    home = environ.get("AGENTIHOOKS_HOME") or str(Path.home() / ".agentihooks")
    return Path(home) / "swarm" / swarm / "gate-modes.json"


def _read_gates(swarm: str, environ: MutableMapping[str, str]) -> dict[str, str] | None:
    """Read the swarm config's gates entry; an unreadable config falls back to the environment."""
    import redis

    from scripts.swarm.hook_redis import client
    from scripts.swarm.store import RedisStore, SwarmError

    try:
        return RedisStore(client(environ)).config(swarm).gates
    except (redis.RedisError, SwarmError, ValueError):
        return None
