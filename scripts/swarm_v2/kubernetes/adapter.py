"""The Kubernetes backend behind the runtime protocol: an admitted spawn becomes one journaled Pod create."""

from collections.abc import Callable

from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm.tick import Placed
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.runtime.base import Capability, Outcome, Recovery, SpawnRequest, Status, Unqualified
from scripts.swarm_v2.runtime.operations import SPAWN, Operation, OperationConflict, OperationRequest, Phase

STATUSES = {Phase.APPLIED: Status.OK, Phase.UNKNOWN: Status.AMBIGUOUS, Phase.REFUSED: Status.REFUSED}
CREATE = "create"


class KubernetesRuntime:
    backend = BACKEND
    capabilities = frozenset({Capability.SPAWN})

    def __init__(self, execute: Callable[[OperationRequest], Operation], launch: Callable[[SpawnRequest], dict]):
        self.execute, self.launch = execute, launch

    def spawn(self, request: SpawnRequest) -> Outcome:
        execution_id, generation = request.task.get("execution_id"), request.task.get("generation")
        if not execution_id or not generation:
            return Outcome(SPAWN, Status.REFUSED, BACKEND, detail=Unqualified.NO_EXECUTION.value)
        payload = {**self.launch(request), "execution_id": execution_id, "generation": generation}
        try:
            operation = self.execute(OperationRequest(execution_id, generation, SPAWN, payload, CREATE))
        except (SwarmError, OperationConflict) as error:
            return Outcome(SPAWN, Status.REFUSED, BACKEND, detail=str(error))
        status = STATUSES.get(operation.phase, Status.UNAVAILABLE)
        if status is not Status.OK:
            detail = f"spawn operation {operation.operation_id} is {operation.phase.value}"
            return Outcome(SPAWN, status, BACKEND, detail=detail)
        placed = Placed("", payload["harness"], placement=BACKEND, profile=payload["profile"])
        return Outcome(SPAWN, Status.OK, BACKEND, placed)

    def observe(self, agent: AgentRecord) -> Outcome:
        return self._unsupported("observe")

    def command(self, agent: AgentRecord, text: str) -> Outcome:
        return self._unsupported("command")

    def drain(self, agent: AgentRecord) -> Outcome:
        return self._unsupported("drain")

    def terminate(self, agent: AgentRecord, homes: tuple = ()) -> Outcome:
        return self._unsupported("terminate")

    def recover(self, agent: AgentRecord, mode: Recovery, config: object, text: str) -> Outcome:
        return self._unsupported("recover")

    def _unsupported(self, operation: str) -> Outcome:
        return Outcome(operation, Status.UNSUPPORTED, BACKEND, detail=f"{BACKEND} lacks {operation}")
