from scripts.hub.registry import Reactor

INTERVAL_MS = 60_000
CLAIMS = "spoke-work"
WITHDRAWN = {"state": "withdrawn", "by": "readiness"}


class Readiness:
    def __init__(self, bus, store=None, load=None, clock=None, policy=None):
        self._bus = bus

    def reactor(self) -> Reactor:
        return Reactor("readiness", "swarm.*.readiness.requested", lambda _: None, singleton=True, poll=self.poll)

    def poll(self, guard=lambda: True) -> None:
        return None
