from collections.abc import Callable
from dataclasses import replace

from hooks.proc import processes
from scripts.swarm.store import (
    APP_SERVER_TRANSPORT,
    LEGACY_TRANSPORT,
    AgentRecord,
    RedisStore,
    SwarmConfig,
    SwarmError,
    connect,
)
from scripts.swarm.tick import Placed, Runtime, SpawnError
from scripts.swarm_v2.runtime import process


def transport(config: SwarmConfig, harness: str) -> str:
    return getattr(config, "codex_inbox_transport", LEGACY_TRANSPORT) if harness == "codex" else ""


def arguments(config: SwarmConfig, harness: str, task: dict) -> list[str]:
    if harness != "codex":
        return []
    transport = getattr(config, "codex_inbox_transport", LEGACY_TRANSPORT)
    result = ["--codex-transport", transport]
    if transport == APP_SERVER_TRANSPORT:
        result += ["--seat", task["seat"]]
        previous = task.get("previous_execution_id")
        if previous:
            result += ["--previous-execution", previous]
    return result


def complete(config: SwarmConfig, name: str, placed: Placed, terminate: Callable[[str], object]) -> Placed:
    if placed.harness != "codex":
        return placed
    transport = getattr(config, "codex_inbox_transport", LEGACY_TRANSPORT)
    if transport == LEGACY_TRANSPORT:
        return replace(placed, transport=transport)
    try:
        execution = next((row for row in connect().execution_occupants(config.slug).values() if row.name == name), None)
        if execution is None or execution.transport != transport:
            raise SwarmError("the Codex app server launch has no matching admitted execution")
        return replace(placed, transport=transport, execution=execution)
    except Exception as exc:
        terminate(name)
        raise SpawnError(str(exc), pane_id=placed.pane_id) from exc


def admit_legacy(store: RedisStore, slug: str, agent: AgentRecord, runtime: Runtime) -> AgentRecord:
    if agent.harness != "codex" or agent.transport != LEGACY_TRANSPORT or agent.execution_id:
        return agent
    try:
        pid = agent.profile_decision.get("validation", {}).get("pid")
        target = process.launch_target(pid, process.local_namespace(), processes())
        previous = store.execution_occupants(slug).get(agent.seat)
        from scripts.swarm.commands import hive_id

        return store.start_execution(
            slug, replace(agent, hive=hive_id(), runtime_target=target), previous.execution_id if previous else ""
        )
    except Exception as exc:
        runtime.retire(agent)
        raise SpawnError(str(exc), pane_id=agent.pane_id) from exc
