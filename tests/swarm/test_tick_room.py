from scripts.swarm.store import MASTER, SwarmError
from scripts.swarm_v2.admission import PendingAdmission, Policy, Resources, Template
from tests.swarm.test_tick import FakeRuntime

ROOM = "no hive for eng has room: h1 max agents: 1 of 1 agents on the hive, fits 0 more"
GENERAL = Template("general", Resources(16384, 4000))


class RacingRuntime(FakeRuntime):
    def __init__(self, saved):
        super().__init__()
        self.taken = False
        self.admission = PendingAdmission(saved, Policy(3, 3, 100, (GENERAL,), Resources(1, 1)), lambda s: 3)

    def placement_refusal(self, lane, task):
        return ROOM if self.taken and lane == "eng" else ""

    def spawn(self, config, lane, name, task):
        if lane == MASTER:
            return super().spawn(config, lane, name, task)
        self.taken = True
        raise SwarmError(ROOM)
