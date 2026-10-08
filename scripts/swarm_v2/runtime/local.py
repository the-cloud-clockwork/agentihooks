"""The local herdr runtime behind the runtime protocol: each operation runs the existing HerdrRuntime call."""

import subprocess

from scripts.agent_choice import ALL_FULL
from scripts.swarm.tick import SpawnError
from scripts.swarm_v2.runtime.base import LOCAL, Capability, Outcome, Recovery, SpawnRequest, Status, foreign


def _failed(operation: str, exc: SpawnError) -> Outcome:
    text = str(exc)
    if isinstance(exc.__cause__, subprocess.TimeoutExpired):
        status = Status.AMBIGUOUS
    elif text.startswith("unsupported"):
        status = Status.UNSUPPORTED
    elif text == ALL_FULL:
        status = Status.UNAVAILABLE
    else:
        status = Status.REFUSED
    return Outcome(operation, status, LOCAL, detail=text)


class LocalHerdrRuntime:
    backend = LOCAL
    capabilities = frozenset(
        {
            Capability.SPAWN,
            Capability.OBSERVE,
            Capability.COMMAND,
            Capability.TERMINATE,
            Capability.RECOVER,
            Capability.NATIVE_RESUME,
        }
    )

    def __init__(self, herdr):
        self.herdr = herdr

    def spawn(self, request: SpawnRequest) -> Outcome:
        try:
            placed = self.herdr.spawn(request.config, request.lane, request.name, request.task)
        except SpawnError as exc:
            return _failed("spawn", exc)
        return Outcome("spawn", Status.OK, LOCAL, placed)

    def observe(self, agent) -> Outcome:
        refused = foreign(self, "observe", agent)
        if refused:
            return refused
        observed = self.herdr.observe(agent)
        status = Status.AMBIGUOUS if observed.state == "unknown" else Status.OK
        return Outcome("observe", status, LOCAL, observed)

    def command(self, agent, text: str) -> Outcome:
        refused = foreign(self, "command", agent)
        if refused:
            return refused
        self.herdr.nudge(agent, text)
        return Outcome("command", Status.OK, LOCAL, "accepted")

    def drain(self, agent) -> Outcome:
        return Outcome("drain", Status.UNSUPPORTED, LOCAL, detail="local herdr has no drain")

    def terminate(self, agent, homes: tuple = ()) -> Outcome:
        refused = foreign(self, "terminate", agent)
        if refused:
            return refused
        if self.herdr.retire(agent, homes=homes):
            return Outcome("terminate", Status.OK, LOCAL)
        refusal = self.herdr.refusal(agent)
        return Outcome("terminate", Status.REFUSED, LOCAL, refusal, refusal["refusal"])

    def recover(self, agent, mode: Recovery, config=None, text: str = "") -> Outcome:
        refused = foreign(self, "recover", agent)
        if refused:
            return refused
        if mode is Recovery.REATTACH:
            placed = self.herdr.recover(agent.name)
            status = Status.OK if placed.pane_id else Status.UNAVAILABLE
            return Outcome("recover", status, LOCAL, placed)
        try:
            placed = self.herdr.resume(config, agent, text)
        except SpawnError as exc:
            return _failed("recover", exc)
        return Outcome("recover", Status.OK, LOCAL, placed)
