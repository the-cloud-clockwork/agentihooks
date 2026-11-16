from scripts import serena_router_daemon
from scripts.swarm_v2.runtime.base import SpawnRequest


def ungated_request(event: object) -> SpawnRequest:
    # ruleid: hive-spawn-before-serena-router
    return SpawnRequest(event, "eng", "eng@rig-0001", {})


def gated_request(event: object) -> SpawnRequest:
    if not serena_router_daemon.ensure():
        raise RuntimeError("spawn unavailable: serena")
    # ok: hive-spawn-before-serena-router
    return SpawnRequest(event, "eng", "eng@rig-0001", {})
