from typing import Any


class Unguarded:
    def bindings(self, agents: list[Any], session: Any, facts: dict[str, Any]) -> None:
        for agent in agents:
            if session is not None:
                facts[agent.name] = {"pid": session}
            elif agent.validated:
                # ruleid: local-session-miss-reads-as-no-process
                facts[agent.name] = {"process": False}


class Guarded:
    def _listed_elsewhere(self, name: str) -> bool:
        return bool(name)

    def bindings(self, agents: list[Any], session: Any, facts: dict[str, Any]) -> None:
        for agent in agents:
            if session is not None:
                facts[agent.name] = {"pid": session}
            elif agent.validated:
                if not self._listed_elsewhere(agent.name):
                    # ok: local-session-miss-reads-as-no-process
                    facts[agent.name] = {"process": False}
