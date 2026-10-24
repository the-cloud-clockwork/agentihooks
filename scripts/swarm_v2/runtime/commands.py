import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Protocol

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.runtime.base import Outcome, Status
from scripts.swarm_v2.runtime.operations import (
    Observation,
    Operation,
    OperationConflict,
    OperationRequest,
    Operations,
    OperationTransport,
    Phase,
)


class Action(StrEnum):
    ATTACH = "attach"
    DETACH = "detach"
    ANSWER = "answer"
    DRAIN = "drain"
    CANCEL = "cancel"
    FORCE_STOP = "force-stop"


OPERATIONS = {
    Action.ATTACH: "recover",
    Action.DETACH: "command",
    Action.ANSWER: "command",
    Action.DRAIN: "drain",
    Action.CANCEL: "command",
    Action.FORCE_STOP: "terminate",
}
STATUSES = {Phase.APPLIED: Status.OK, Phase.REFUSED: Status.REFUSED}


class Role(StrEnum):
    OPERATOR = "operator"
    MASTER = "master"


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role
    execution_id: str = ""
    generation: int = 0


@dataclass(frozen=True)
class Request:
    execution_id: str
    generation: int
    action: Action
    key: str
    text: str = ""


@dataclass(frozen=True)
class Controls:
    execution_id: str
    generation: int
    backend: str
    state: str
    actions: tuple[Action, ...]


class CommandTransport(OperationTransport, Protocol):
    """Detach edits only viewer registration; cancel requests checkpoint/stop, never releases task authority.

    Apply must fence execution id, generation and runtime target at the effect boundary;
    observe must identify the operation, including a lost acknowledgement, without replaying it.
    """

    commands: frozenset[Action]


class _GuardedTransport:
    def __init__(self, transport: CommandTransport, admitted: Callable[[], bool]):
        self.transport = transport
        self.backend = transport.backend
        self.admitted = admitted

    def observe_operation(self, operation: Operation) -> Observation:
        return self.transport.observe_operation(operation)

    def apply_operation(self, operation: Operation, payload: dict) -> Observation:
        if not self.admitted():
            return Observation(Phase.REFUSED)
        return self.transport.apply_operation(operation, payload)


class Commands:
    """Authenticate scoped credentials into operator identity or a registered master incarnation before lookup."""

    def __init__(
        self,
        store: Any,
        transports: Iterable[CommandTransport],
        authenticate: Callable[[str, str], Principal | None],
        enabled: bool = True,
    ):
        self.store = store
        self.transports = {transport.backend: transport for transport in transports}
        self.authenticate = authenticate
        self.enabled = enabled

    def controls(self, slug: str, seat: str, credential: str) -> Controls | None:
        if self._actor(slug, credential) is None:
            return None
        agent = self.store.execution_registry.occupants(slug).get(seat)
        if agent is None:
            return None
        transport = self.transports.get(agent.runtime_backend)
        actions = tuple(action for action in Action if transport and self.enabled and action in transport.commands)
        return Controls(agent.execution_id, agent.generation, agent.runtime_backend, agent.state, actions)

    def execute(self, slug: str, request: Request, credential: str) -> Outcome:
        actor = self._actor(slug, credential)
        agent = None
        if actor is None:
            result = Outcome(
                request.action, Status.REFUSED, "", detail="authenticated operator or current master required"
            )
        elif not _valid(request):
            result = Outcome(request.action, Status.REFUSED, "", detail="invalid runtime command")
        else:
            agent = next(
                (
                    a
                    for a in self.store.execution_registry.occupants(slug).values()
                    if a.execution_id == request.execution_id
                ),
                None,
            )
            result = self._execute(slug, request, agent, actor, credential)
        self._audit(slug, request, actor, result)
        return result

    def _actor(self, slug: str, credential: str) -> Principal | None:
        principal = self.authenticate(slug, credential)
        if not isinstance(principal, Principal) or not isinstance(principal.name, str) or not principal.name:
            return None
        if principal.role is Role.OPERATOR:
            return principal
        if principal.role is not Role.MASTER or type(principal.generation) is not int:
            return None
        masters = self.store.execution_registry.occupants(slug).values()
        if any(
            a.name == principal.name
            and a.execution_id == principal.execution_id
            and a.generation == principal.generation
            and a.lane == "master"
            and a.state != "finished"
            for a in masters
        ):
            return principal
        return None

    def _execute(
        self, slug: str, request: Request, agent: AgentRecord | None, actor: Principal, credential: str
    ) -> Outcome:
        if agent is None or agent.generation != request.generation:
            return Outcome(request.action, Status.REFUSED, "", detail="execution generation is stale")
        backend = agent.runtime_backend
        transport = self.transports.get(backend)
        if transport is None:
            return Outcome(request.action, Status.UNAVAILABLE, backend, detail="selected backend is unavailable")
        if not self.enabled or request.action not in transport.commands:
            return Outcome(
                request.action, Status.UNSUPPORTED, backend, detail="command is unsupported by the selected backend"
            )
        payload = {"command": request.action.value, "text": request.text}
        operation = OperationRequest(
            agent.execution_id, request.generation, OPERATIONS[request.action], payload, request.key
        )

        def admitted():
            current = self.store.execution_registry.occupants(slug).get(agent.seat)
            return current == agent and self._actor(slug, credential) == actor

        guarded = _GuardedTransport(transport, admitted)
        try:
            accepted = Operations(self.store, [guarded]).execute(slug, operation)
        except OperationConflict:
            return Outcome(
                request.action, Status.REFUSED, backend, detail="runtime command conflicts with current authority"
            )
        return Outcome(request.action, STATUSES.get(accepted.phase, Status.AMBIGUOUS), backend, accepted)

    def _audit(self, slug: str, request: Request, actor: Principal | None, result: Outcome) -> None:
        row = {
            "actor": asdict(actor) if actor is not None else None,
            "execution_id": request.execution_id,
            "generation": request.generation,
            "command": request.action,
            "backend": result.backend,
            "outcome": result.status,
        }
        dimension = json.dumps([result.backend, request.action, result.status])
        with self.store.redis.pipeline() as pipe:
            pipe.rpush(self.store.key(slug, "runtime-command-audit"), json.dumps(row))
            pipe.hincrby(self.store.key(slug, "runtime-command-outcomes"), dimension, 1)
            pipe.execute()

    def audit(self, slug: str) -> list[dict]:
        return [
            json.loads(row) for row in self.store.redis.lrange(self.store.key(slug, "runtime-command-audit"), 0, -1)
        ]

    def runtime_commands_by_outcome(self, slug: str) -> dict[str, int]:
        return {
            key: int(value)
            for key, value in self.store.redis.hgetall(self.store.key(slug, "runtime-command-outcomes")).items()
        }


def _valid(request: Request) -> bool:
    return (
        isinstance(request.action, Action)
        and isinstance(request.execution_id, str)
        and bool(request.execution_id)
        and type(request.generation) is int
        and request.generation > 0
        and isinstance(request.key, str)
        and bool(request.key)
        and isinstance(request.text, str)
        and (bool(request.text) if request.action is Action.ANSWER else request.text == "")
    )
