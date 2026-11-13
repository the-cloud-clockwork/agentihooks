from typing import Any

from hooks.proc import processes
from scripts.gates import log as gate_log
from scripts.swarm import metrics
from scripts.swarm_v2.runtime import codex


def gate_row(slug: str, row: Any) -> None:
    # ruleid: tick-spawn-metrics-processes
    gate_log.append(slug, row)


def dispatch_rows(table: Any, rows: list[Any], now_ms: int) -> list[str]:
    # ruleid: tick-spawn-metrics-processes
    return metrics.record(table, rows, now_ms)


def table() -> Any:
    # ruleid: tick-spawn-metrics-processes
    return processes()


def admit(store: Any, slug: str, agent: Any, runtime: Any) -> Any:
    # ruleid: tick-spawn-metrics-processes
    return codex.admit_legacy(store, slug, agent, runtime)


def through_the_ports(ports: Any, store: Any, slug: str, agent: Any, runtime: Any, table: Any, rows: list[Any]) -> Any:
    # ok: tick-spawn-metrics-processes
    ports.log(slug, rows)
    # ok: tick-spawn-metrics-processes
    ports.record(table, rows)
    # ok: tick-spawn-metrics-processes
    return codex.admit_legacy(store, slug, agent, runtime, ports.table)
