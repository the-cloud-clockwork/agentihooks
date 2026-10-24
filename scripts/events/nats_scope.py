import re
import threading
from typing import TYPE_CHECKING

from nats.js import JetStreamContext

from .bus import StateStore, WorkQueue
from .config import SCOPED_STATE_BUCKETS, QueuePolicy
from .nats import NatsEventLog, NatsWorkQueue, _seconds
from .nats_state import ScopedStateStore
from .scope import ScopedLog, queue_lane
from .subjects import PREFIX, stream_name, subject

if TYPE_CHECKING:
    from .nats import NatsBus


async def discover_swarms(js: JetStreamContext) -> list[str]:
    offset, swarms = 0, []
    while True:
        page = await js.streams_info_iterator(offset=offset)
        for info in page:
            name = info.config.name
            matched = re.fullmatch(r"AGENTIHOOKS_([0-9a-f]+)_EVENTS", name)
            if matched:
                swarms.append(bytes.fromhex(matched[1]).decode())
        offset += len(page.streams)
        if offset >= page.total:
            return swarms
        if not page.streams:
            raise RuntimeError("NATS stream inventory did not advance")


class NatsScopedBus:
    def __init__(self, root: "NatsBus", swarm: str):
        self._root, self._swarm = root, swarm
        self._queues: dict[str, NatsWorkQueue] = {}
        self._buckets: dict[str, ScopedStateStore] = {}
        self._lock = threading.Lock()
        if root._config.provision:
            for resource, family in [("EVENTS", "events"), ("DEAD", "dead")]:
                root._loop.run(
                    root._js.add_stream(
                        name=stream_name(swarm, resource),
                        subjects=[f"{subject(swarm, family)}.>"],
                        duplicate_window=_seconds(root._config.dedupe_window_ms),
                    )
                )
            for bucket in SCOPED_STATE_BUCKETS:
                self.state(bucket)
        self.events = ScopedLog(
            NatsEventLog(
                root._loop, root._js, root._config.queue, stream_name(swarm, "EVENTS"), root._config.provision, swarm
            ),
            swarm,
            "events",
        )
        self.dead = ScopedLog(
            NatsEventLog(
                root._loop, root._js, root._config.queue, stream_name(swarm, "DEAD"), root._config.provision, swarm
            ),
            swarm,
            "dead",
        )

    def for_swarm(self, swarm: str) -> "NatsScopedBus":
        if swarm != self._swarm:
            raise ValueError("a scoped bus cannot change its swarm")
        return self

    def queue(self, name: str, policy: QueuePolicy) -> WorkQueue:
        lane = queue_lane(self._swarm, name)
        with self._lock:
            queue = self._queues.get(lane)
            if queue is None:
                scoped = f"{PREFIX}.{self._swarm}.ready.{lane}"
                queue = self._queues[lane] = NatsWorkQueue(
                    self._root._loop, self._root._js, scoped, policy, self._root._config, self._swarm
                )
            elif queue.policy != policy:
                raise ValueError(f"work queue {name!r} already has another policy")
            return queue

    def state(self, bucket: str) -> StateStore:
        with self._lock:
            store = self._buckets.get(bucket)
            if store is None:
                store = self._buckets[bucket] = ScopedStateStore(self._root, self._swarm, bucket)
            return store

    def request(self, subject: str, data: bytes, timeout_ms: int) -> bytes:
        if not subject.startswith(f"{PREFIX}.{self._swarm}."):
            raise ValueError(f"a scoped bus cannot request outside swarm {self._swarm!r}")
        return self._root.request(subject, data, timeout_ms)

    def close(self) -> None:
        pass
