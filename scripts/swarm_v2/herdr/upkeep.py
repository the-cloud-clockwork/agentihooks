from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from scripts.swarm_v2.herdr.attach import Terminal
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused, Scope
from scripts.swarm_v2.herdr.reconcile import Report

SWITCH = "AGENTIHOOKS_HERDR_REGISTRATIONS"
CLIENT = "herdr"


def switched_on(environ: Mapping[str, str]) -> bool:
    return environ.get(SWITCH, "on") != "off"


@dataclass(frozen=True)
class Attempt:
    scope: Scope
    host_public: str


class Upkeep:
    """Disabled, it runs no herdr command and leaves every saved machine in place."""

    def __init__(self, terminal: Terminal, enabled: bool) -> None:
        self.terminal, self.enabled = terminal, enabled
        self.seen: dict[str, int] | None = None
        self.last = Report()

    def start(self, attempts: Iterable[Attempt]) -> Report | None:
        self.seen = None
        return self.change(attempts)

    def change(self, attempts: Iterable[Attempt]) -> Report | None:
        if not self.enabled:
            return None
        attempts = tuple(attempts)
        current = {attempt.scope.execution_id: attempt.scope.generation for attempt in attempts}
        if current == self.seen:
            return None
        desired, refused = self._routed(attempts)
        try:
            report = self.terminal.registrations.reconcile(desired, self.terminal.qualifier)
        except EndpointRefused as failure:
            return Report(refused={CLIENT: failure.reason})
        report.refused.update(refused)
        self.seen = None if report.refused else current
        self.last = report
        return report

    def _routed(self, attempts: tuple[Attempt, ...]) -> tuple[list[Endpoint], dict[str, str]]:
        desired, refused = [], {}
        for attempt in attempts:
            try:
                desired.append(self.terminal.register(attempt.scope, attempt.host_public))
            except EndpointRefused as failure:
                refused[attempt.scope.execution_id] = failure.reason
        return desired, refused

    def herdr_registration_drift(self) -> int:
        return self.last.herdr_registration_drift()
