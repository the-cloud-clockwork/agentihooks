import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from scripts.swarm_v2.herdr.capabilities import Unreachable
from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.target import Router, Run, Target, pane_conversation
from scripts.swarm_v2.herdr.visibility import CACHED, FRESH_MS, LIVE, UNVERIFIED, View, judged
from scripts.swarm_v2.runtime.commands import Principal, Role


@dataclass(frozen=True)
class Watched:
    target: Target
    view: View
    screen: str = ""


class Viewer:
    """The operator's view of remote agent panes through the server qualified router. Closing it or losing a machine
    only changes what the view shows: a failed check keeps the last screen as cached data, never reports the agent
    gone, and no viewer operation launches, stops or closes anything."""

    def __init__(
        self,
        router: Router,
        run: Run,
        caller: Principal,
        clock: Callable[[], float] = time.time,
        *,
        fresh_ms: int = FRESH_MS,
        aggregation: bool = True,
    ) -> None:
        if caller.role is not Role.OPERATOR:
            raise EndpointRefused("unauthorized", "only the operator role opens a terminal view")
        self.router, self.run, self.clock = router, run, clock
        self.fresh_ms, self.aggregation = fresh_ms, aggregation
        self.watched: dict[str, Watched] = {}
        self.latencies: list[float] = []

    def _now(self) -> int:
        return round(self.clock() * 1000)

    def _held(self, agent: str) -> Watched:
        held = self.watched.get(agent)
        if held is None:
            raise EndpointRefused("unwatched", f"{agent} has no terminal in this view")
        return held

    def watch(self, agent: str, machine: str, pane_id: str, conversation_id: str) -> View:
        if not conversation_id:
            raise EndpointRefused("conversation_required", f"{agent}'s terminal view needs its conversation id")
        target = self.router.resolve(machine, pane_id, conversation_id)
        view = View(agent, machine, target.pane_id, UNVERIFIED, fresh_ms=self.fresh_ms)
        self.watched[agent] = Watched(target, view)
        return view

    def _read(self, target: Target, operation: str) -> str:
        try:
            done = self.run(self.router.command(target, operation))
        except (OSError, subprocess.TimeoutExpired, Unreachable):
            raise EndpointRefused("unreachable", f"{target.pane_id} did not answer {operation}") from None
        if done.returncode != 0:
            raise EndpointRefused("unreachable", f"herdr exited {done.returncode} for {operation} on {target.pane_id}")
        return done.stdout

    def _probe(self, target: Target) -> tuple[Target, str]:
        target = self.router.refresh(target)
        found = pane_conversation(self._read(target, "agent_get"))
        if found is None or not found[1]:
            raise EndpointRefused("conversation_unconfirmed", f"{target.pane_id} reported no conversation")
        if found != (target.pane_id, target.conversation_id):
            raise EndpointRefused("conversation_mismatch", f"{target.pane_id} holds another conversation")
        return target, self._read(target, "pane_read")

    def _check(self, agent: str) -> EndpointRefused | None:
        held = self._held(agent)
        now = self._now()
        try:
            target, screen = self._probe(held.target)
        except EndpointRefused as refused:
            self.watched[agent] = replace(
                held, view=replace(held.view, state=CACHED, checked_at=now, reason=refused.reason)
            )
            return refused
        self.watched[agent] = Watched(
            target, replace(held.view, state=LIVE, checked_at=now, live_at=now, reason=""), screen
        )
        return None

    def refresh(self) -> dict[str, View]:
        if self.aggregation:
            for agent in sorted(self.watched):
                self._check(agent)
        return self.views()

    def views(self) -> dict[str, View]:
        now = self._now()
        return {agent: judged(held.view, now) for agent, held in sorted(self.watched.items())}

    def screen(self, agent: str) -> tuple[str, View]:
        held = self._held(agent)
        return held.screen, judged(held.view, self._now())

    def send(self, agent: str, operation: str, operation_id: str, *args: str) -> subprocess.CompletedProcess | None:
        held = self._held(agent)
        view = judged(held.view, self._now())
        if view.state != LIVE:
            raise EndpointRefused("unverified", f"{agent}'s terminal is {view.state}; reconnect before sending input")
        return self.router.send(held.target, operation, operation_id, *args)

    def reconnect(self, agent: str) -> Target:
        started = self._now()
        refused = self._check(agent)
        if refused is not None:
            raise refused
        self.latencies.append((self._now() - started) / 1000)
        return self.watched[agent].target

    def detach(self) -> None:
        self.watched.clear()

    def forget(self, agent: str) -> None:
        self.watched.pop(agent, None)

    def terminal_reconnect_latency_seconds(self) -> list[float]:
        return list(self.latencies)
