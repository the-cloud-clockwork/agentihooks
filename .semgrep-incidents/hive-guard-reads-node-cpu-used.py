from typing import Any

SWARM_PERCENT = 40
HOST_PERCENT = 80


def guard_on_node_used(cpus: float, load5: float, used: float) -> bool:
    # ruleid: hive-guard-reads-node-cpu-used
    return 100 * load5 > HOST_PERCENT * cpus and 100 * used >= SWARM_PERCENT * cpus


def guard_on_node_used_name(cpus: float, used_cpus: float) -> bool:
    # ruleid: hive-guard-reads-node-cpu-used
    return 100 * used_cpus >= SWARM_PERCENT * cpus


def guard_on_reading_index(measured: dict[str, Any]) -> bool:
    # ruleid: hive-guard-reads-node-cpu-used
    return 100 * measured["used_cpus"] >= SWARM_PERCENT * measured["cpus"]


def guard_on_reading_get(measured: dict[str, Any]) -> bool:
    # ruleid: hive-guard-reads-node-cpu-used
    return 100 * measured.get("used_cpus", 0.0) >= SWARM_PERCENT * measured["cpus"]


def guard_on_own_cpu(cpus: float, load5: float, used: float, pod_load: float | None) -> bool:
    own = used if pod_load is None else pod_load
    # ok: hive-guard-reads-node-cpu-used
    return 100 * load5 > HOST_PERCENT * cpus and 100 * own >= SWARM_PERCENT * cpus


def used_line_under_host_percent(cpus: float, used: float) -> bool:
    # ok: hive-guard-reads-node-cpu-used
    return 100 * used >= HOST_PERCENT * cpus
