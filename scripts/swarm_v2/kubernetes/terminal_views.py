from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_v2.herdr import visibility
from scripts.swarm_v2.herdr.capabilities import Unreachable
from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.viewer import Viewer
from scripts.swarm_v2.herdr.visibility import View
from scripts.swarm_v2.kubernetes.prompts import agent_target
from scripts.swarm_v2.kubernetes.watch import BACKEND


class TerminalViews:
    """Each controller pass finds every remote agent's pane on the machine registered for its execution and
    generation, refreshes the operator viewer and publishes the views. An agent whose pane cannot be found keeps its
    last view; only an agent that left the swarm is dropped."""

    def __init__(self, store: RedisStore, slug: str, viewer: Viewer) -> None:
        self.store, self.slug, self.viewer = store, slug, viewer

    def __call__(self) -> dict[str, View]:
        occupants = self.store.execution_occupants(self.slug).values()
        remote = {agent.name: agent for agent in occupants if agent.runtime_backend == BACKEND}
        for name in set(self.viewer.watched) - set(remote):
            self.viewer.forget(name)
        for agent in remote.values():
            self._watch(agent)
        views = self.viewer.refresh()
        visibility.publish(self.store, self.slug, views.values())
        return views

    def _watch(self, agent: AgentRecord) -> None:
        held = self.viewer.watched.get(agent.name)
        wanted = (f"{agent.execution_id}/{agent.generation}", agent.conversation_id)
        if held is not None and (held.target.incarnation, held.target.conversation_id) == wanted:
            return
        try:
            target, label = agent_target(self.viewer.router, agent)
            self.viewer.watch(agent.name, label, target.pane_id, agent.conversation_id)
        except (EndpointRefused, Unreachable):
            pass
