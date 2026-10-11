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


class Upkeep:
    """Disabled, it runs no herdr command and leaves every saved machine in place."""

    def __init__(self, terminal: Terminal, enabled: bool) -> None:
        self.terminal, self.enabled = terminal, enabled
        self.due, self.seen = True, {}
        self.last = Report()

    def start(self, attempts: Iterable[Attempt]) -> Report | None:
        self.due = True
        return self.change(attempts)

    def change(self, attempts: Iterable[Attempt]) -> Report | None:
        if not self.enabled:
            return None
        attempts = tuple(attempts)
        current = {attempt.scope.execution_id: attempt.scope.generation for attempt in attempts}
        if not self.due and current == self.seen:
            return None
        try:
            report = self.terminal.sync(((attempt.scope, attempt.host_public) for attempt in attempts), enabled=True)
        except EndpointRefused as failure:
            self.due = True
            return Report(refused={CLIENT: failure.reason})
        self.due, self.seen = bool(report.refused), current
        self.last = report
        return report

    def herdr_registration_drift(self) -> int:
        return self.last.herdr_registration_drift()
