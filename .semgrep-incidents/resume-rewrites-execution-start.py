from dataclasses import replace
from typing import Any


def reopened(store: Any, runtime: Any, config: Any, agent: Any, text: str, at: int) -> None:
    placed = runtime.resume(config, agent, text)
    # ruleid: resume-rewrites-execution-start
    store.put_agent("sw", replace(agent, pane_id=placed.pane_id, started_at=at))


def restarted(store: Any, runtime: Any, config: Any, agent: Any, task: dict, at: int, placed_record: Any) -> None:
    placed = runtime.spawn(config, agent.lane, agent.name, task)
    # ruleid: resume-rewrites-execution-start
    store.put_agent("sw", replace(placed_record(agent, placed), started_at=at, idle_ticks=0))


def kept(store: Any, runtime: Any, config: Any, agent: Any, text: str, at: int) -> None:
    placed = runtime.resume(config, agent, text)
    # ok: resume-rewrites-execution-start
    store.put_agent("sw", replace(agent, pane_id=placed.pane_id, launched_at=at))


def created(store: Any, agent: Any, at: int) -> None:
    # ok: resume-rewrites-execution-start
    store.put_agent("sw", replace(agent, started_at=at))
