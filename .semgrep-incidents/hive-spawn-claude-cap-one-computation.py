from typing import Any

from scripts import session_bands
from scripts.swarm.runtime import HerdrRuntime, worker_runtime
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime


def band_spawn_runtime() -> Any:
    # ruleid: hive-spawn-claude-cap-one-computation
    return LocalHerdrRuntime(HerdrRuntime())


def worker_spawn_runtime() -> Any:
    # ok: hive-spawn-claude-cap-one-computation
    return LocalHerdrRuntime(worker_runtime())


def own_scaled_cap(five: float, week: float, ceiling: int) -> int | None:
    # ruleid: hive-spawn-claude-cap-one-computation
    return session_bands.scaled_cap(five, week, ceiling)


def own_ceiling(environ: dict[str, str]) -> int:
    # ruleid: hive-spawn-claude-cap-one-computation
    return session_bands.hive_ceiling(environ)


def shared_cap(five: float, week: float, environ: dict[str, str]) -> int | None:
    # ok: hive-spawn-claude-cap-one-computation
    return session_bands.worker_cap(five, week, environ)
