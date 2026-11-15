from typing import Any


def unguarded(agents: list[Any], session: Any, facts: dict[str, Any]) -> None:
    for agent in agents:
        if session is not None:
            facts[agent.name] = {"pid": session}
        elif agent.validated:
            # ruleid: local-session-miss-reads-as-no-process
            facts[agent.name] = {"process": False}


def guarded(agents: list[Any], session: Any, facts: dict[str, Any], listed: set[str]) -> None:
    for agent in agents:
        if session is not None:
            facts[agent.name] = {"pid": session}
        elif agent.validated and agent.name not in listed:
            # ok: local-session-miss-reads-as-no-process
            facts[agent.name] = {"process": False}
