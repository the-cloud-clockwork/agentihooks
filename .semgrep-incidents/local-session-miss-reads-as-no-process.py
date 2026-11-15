from typing import Any


def unguarded(agents: list[Any], session: Any, facts: dict[str, Any]) -> None:
    for agent in agents:
        if session is not None:
            facts[agent.name] = {"pid": session}
        elif agent.validated:
            # ruleid: local-session-miss-reads-as-no-process
            facts[agent.name] = {"process": False}


def guarded(runtime: Any, agents: list[Any], session: Any, facts: dict[str, Any]) -> None:
    for agent in agents:
        if session is not None:
            facts[agent.name] = {"pid": session}
        elif agent.validated:
            if not runtime._listed_elsewhere(agent.name):
                # ok: local-session-miss-reads-as-no-process
                facts[agent.name] = {"process": False}
