from dataclasses import replace
from typing import Any


def resumed(store: Any, runtime: Any, config: Any, agent: Any, text: str, at: int) -> None:
    placed = runtime.resume(config, agent, text)
    # ruleid: master-resume-rewrites-start
    record = replace(agent, pane_id=placed.pane_id, started_at=at)
    store.put_agent("sw", record)


def kept(store: Any, runtime: Any, config: Any, agent: Any, text: str, at: int) -> None:
    placed = runtime.resume(config, agent, text)
    # ok: master-resume-rewrites-start
    record = replace(agent, pane_id=placed.pane_id, launched_at=at)
    store.put_agent("sw", record)


def spawned(store: Any, agent: Any, at: int) -> None:
    # ok: master-resume-rewrites-start
    record = replace(agent, started_at=at)
    store.put_agent("sw", record)
