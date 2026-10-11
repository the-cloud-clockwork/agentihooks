from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from scripts.swarm_v2.herdr.attach import Terminal
from scripts.swarm_v2.herdr.endpoints import EndpointRefused, Scope
from scripts.swarm_v2.herdr.reconcile import Report

SWITCH = "AGENTIHOOKS_HERDR_REGISTRATIONS"
CLIENT = "herdr"


def switched_on(environ: Mapping[str, str]) -> bool:
    return environ.get(SWITCH) != "off"


@dataclass(frozen=True)
class Attempt:
    scope: Scope
    host_public: str


def _current(attempts: tuple[Attempt, ...]) -> dict[str, int]:
    return {attempt.scope.execution_id: attempt.scope.generation for attempt in attempts}


class Upkeep:
    def __init__(self, terminal: Terminal) -> None:
        self.terminal = terminal
        self.due, self.seen = True, {}
        self.last = Report()

    def start(self, attempts: Iterable[Attempt]) -> Report:
        return self._sync(tuple(attempts))

    def change(self, attempts: Iterable[Attempt]) -> Report | None:
        attempts = tuple(attempts)
        if not self.due and _current(attempts) == self.seen:
            return None
        return self._sync(attempts)

    def _sync(self, attempts: tuple[Attempt, ...]) -> Report:
        try:
            report = self.terminal.sync(((attempt.scope, attempt.host_public) for attempt in attempts), enabled=True)
        except EndpointRefused as failure:
            self.due = True
            return Report(refused={CLIENT: failure.reason})
        self.due, self.seen = bool(report.refused), _current(attempts)
        self.last = report
        return report

    def herdr_registration_drift(self) -> int:
        return self.last.herdr_registration_drift()
