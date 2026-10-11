import uuid
from collections.abc import Callable
from dataclasses import replace

from scripts.swarm.delivery import marked
from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.herdr.capabilities import Unreachable
from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.target import Router, Target
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.runtime.base import Outcome, Status, Unqualified

COMMAND = "command"
PROMPT = "agent_prompt"
AMBIGUOUS = "ambiguous_delivery"


def operation_id() -> str:
    return f"prompt-{uuid.uuid4().hex}"


class TerminalPrompts:
    """A remote agent is prompted only on the saved machine the controller registered for its execution and
    generation, in the one agent pane that machine's server lists."""

    def __init__(self, router: Router, new_id: Callable[[], str] = operation_id) -> None:
        self.router, self.new_id = router, new_id

    def command(self, agent: AgentRecord, text: str) -> Outcome:
        if not agent.execution_id or not agent.generation:
            return Outcome(COMMAND, Status.REFUSED, BACKEND, detail=Unqualified.NO_EXECUTION.value)
        try:
            self.router.send(self._target(agent), PROMPT, self.new_id(), marked(text))
        except EndpointRefused as refused:
            status = Status.AMBIGUOUS if refused.reason == AMBIGUOUS else Status.REFUSED
            return Outcome(COMMAND, status, BACKEND, detail=f"terminal {refused.reason}: {refused}")
        except Unreachable as error:
            return Outcome(COMMAND, Status.REFUSED, BACKEND, detail=f"terminal unreachable: {error}")
        except OSError as error:
            return Outcome(COMMAND, Status.AMBIGUOUS, BACKEND, detail=f"terminal journal: {error}")
        return Outcome(COMMAND, Status.OK, BACKEND, "accepted")

    def _target(self, agent: AgentRecord) -> Target:
        record = self.router.store.load().get(agent.execution_id)
        if record is None or record.profile_id is None:
            raise EndpointRefused("unregistered", f"{agent.execution_id} has no saved herdr machine")
        server = Target(record.profile_id, record.session, f"{agent.execution_id}/{agent.generation}", "")
        panes = self.router.agent_panes(server)
        if len(panes) != 1:
            raise EndpointRefused("agent_pane", f"{server.incarnation} lists {len(panes)} agent panes, not one")
        return replace(server, pane_id=panes[0], conversation_id=agent.conversation_id)
