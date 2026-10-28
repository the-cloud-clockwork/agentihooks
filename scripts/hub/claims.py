import json
import sys

from scripts.events.envelope import Envelope
from scripts.hub.registry import Reactor
from scripts.swarm.store import RedisStore, connect

_COUNT = """
if redis.call('HSETNX', KEYS[1], ARGV[1], ARGV[2]) == 0 then
    return 0
end
redis.call('HSETNX', KEYS[2], ARGV[2], ARGV[3])
return redis.call('HINCRBY', KEYS[2], ARGV[2], 1)
"""


class Claims:
    def __init__(self, store: RedisStore | None = None):
        self.store = store

    def reactor(self) -> Reactor:
        return Reactor("claim-lives", "swarm.*.execution.started", self.handle, singleton=True)

    def handle(self, event: Envelope) -> None:
        if event.type != "execution.started" or event.entity != "execution" or not event.data.get("claim_key"):
            return
        self.store = self.store or connect()
        task = event.data["task"]
        legacy = self.store.redis.hget(self.store.key(event.swarm, "launches"), event.data["name"])
        if legacy and json.loads(legacy)["state"] == "started":
            return
        lives = self.store.redis.eval(
            _COUNT,
            2,
            self.store.key(event.swarm, "counted-executions"),
            self.store.key(event.swarm, "started-lives"),
            json.dumps([event.key, event.version]),
            task,
            self.store.claims(event.swarm, task),
        )
        if lives:
            print(
                f"hub: claim life swarm={event.swarm} task={task} execution={event.key} lives={lives}",
                file=sys.stderr,
            )
