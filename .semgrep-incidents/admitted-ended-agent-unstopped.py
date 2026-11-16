from typing import Any


def _reap_admitted(judging: Any, agent: Any, tick: Any) -> list[str]:
    # ruleid: admitted-ended-agent-unstopped
    if agent.name not in judging.live or agent.state == "retiring" or tick._ended(agent, judging.rows):
        return []
    return tick._watch_agent(agent)


def reap_admitted_agent(judging: Any, agent: Any, _ended: Any, watch: Any) -> list[str]:
    # ruleid: admitted-ended-agent-unstopped
    if _ended(agent, judging.rows):
        return []
    return watch(agent)


def _reap_admitted_stopping(judging: Any, agent: Any, tick: Any, stop_ended: Any) -> list[str]:
    if agent.state == "retiring":
        return []
    # ok: admitted-ended-agent-unstopped
    if tick._ended(agent, judging.rows):
        return stop_ended(judging.slug, agent)
    if agent.name not in judging.live:
        return []
    return tick._watch_agent(agent)


def _reap_agent(judging: Any, agent: Any, tick: Any) -> list[str]:
    # ok: admitted-ended-agent-unstopped
    if tick._ended(agent, judging.rows):
        return []
    return tick._watch_agent(agent)
