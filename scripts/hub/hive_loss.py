import json
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace

from scripts.events.bus import Bus, StateConflict
from scripts.events.envelope import Envelope
from scripts.hive import execution_events, presence
from scripts.hub import lease
from scripts.hub.registry import Reactor

BUCKET = "hub-hive-loss"
INDEX = "started"
REASON = "hive-lost"
PRUNE_MS = 60_000
# A hive is judged lost only after this hub watched presence for a full TTL without a gap longer than one beat.
GAP_MS = presence.BEAT_SECONDS * 1000


class HiveLoss:
    def __init__(self, bus: Bus, clock: lease.Clock = lease.wall_ms):
        self._bus = bus
        self._state = bus.state(BUCKET)
        self._clock = clock
        self._emit = execution_events.emit
        self._pruned = self._watching = self._polled = clock()

    def reactor(self) -> Reactor:
        return Reactor("hive-loss", "swarm.*.execution.started", self.handle, singleton=True, poll=self.poll)

    def handle(self, event: Envelope) -> None:
        key = event.data.get("claim_key")
        if event.type != "execution.started" or not key or not event.data.get("hive"):
            return

        def add(rows: dict) -> bool:
            if key in rows and rows[key]["data"]["claim_revision"] >= event.data["claim_revision"]:
                return False
            rows[key] = event.to_dict()
            return True

        self._edit(add)

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        now = self._clock()
        if now - self._polled > GAP_MS:
            self._watching = now
        self._polled = now
        entry = self._state.get(INDEX)
        prune = now >= self._pruned + PRUNE_MS
        by_hive = defaultdict(list)
        for key, raw in (json.loads(entry.value) if entry else {}).items():
            by_hive[(raw["swarm"], raw["data"]["hive"])].append((key, Envelope.from_dict(raw)))
        done, losses = [], []
        for (swarm, hive), starts in by_hive.items():
            if not guard():
                break
            lost = now - self._watching >= presence.TTL_MS and presence.lost(self._bus.for_swarm(swarm), hive, now)
            if lost or prune:
                settled, ended = self._settle(starts, lost, now)
                done += settled
                if ended:
                    losses.append(f"hub: hive {hive} of swarm {swarm} lost; exited {', '.join(ended)}")
        else:
            if prune:
                self._pruned = now
        if done:

            def drop(rows: dict) -> bool:
                found = [key for key in done if key in rows]
                for key in found:
                    del rows[key]
                return bool(found)

            self._edit(drop)
            for line in losses:
                print(line, file=sys.stderr)

    def _settle(self, starts: list[tuple[str, Envelope]], lost: bool, now: int) -> tuple[list[str], list[str]]:
        done, ended = [], []
        for key, start in starts:
            claim = self._bus.for_swarm(start.swarm).state("spoke-work").get(key)
            value = json.loads(claim.value) if claim else {}
            running = value.get("state") == "started" and value.get("claim_revision") == start.data["claim_revision"]
            if running and not lost:
                continue
            if running:
                exited = replace(
                    start,
                    id=f"exited:{start.id}",
                    type="execution.exited",
                    time=now,
                    cause=start.id,
                    data={**start.data, "reason": REASON},
                )
                self._emit(self._bus, exited)
                ended.append(f"execution {start.key} of task {start.data.get('task', '')}")
            done.append(key)
        return done, ended

    def _edit(self, change: Callable[[dict], bool]) -> None:
        for _ in range(8):
            entry = self._state.get(INDEX)
            rows = json.loads(entry.value) if entry else {}
            if not change(rows):
                return
            value = json.dumps(rows).encode()
            try:
                if entry:
                    self._state.update(INDEX, value, entry.revision)
                else:
                    self._state.create(INDEX, value)
                return
            except StateConflict:
                continue
        raise StateConflict("hive loss index remained contended")
