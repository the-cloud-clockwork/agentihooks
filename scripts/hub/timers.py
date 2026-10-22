import heapq
import json
from collections.abc import Callable

from scripts.events import deadlines
from scripts.events.bus import Bus, StateConflict
from scripts.events.envelope import Envelope
from scripts.hub import lease
from scripts.hub.registry import Reactor

BUCKET = "hub-timers"
INDEX = "deadlines"


class Timers:
    def __init__(self, bus: Bus, clock: lease.Clock = lease.wall_ms):
        self._events = bus.events
        self._state = bus.state(BUCKET)
        self._clock = clock
        self._revision = 0
        self._pending = []

    def reactor(self) -> Reactor:
        return Reactor("timers", deadlines.SUBJECT, self.handle, singleton=True, poll=self.fire)

    def handle(self, event: Envelope) -> None:
        if event.type == "deadline.expired":
            return
        deadlines.validate(event)
        self._store(event, complete=False)

    def _store(self, event: Envelope, *, complete: bool) -> None:
        for _ in range(8):
            entry = self._state.get(INDEX)
            rows = json.loads(entry.value) if entry else {}
            previous = rows.get(event.key)
            if previous is not None:
                if complete:
                    if previous["request"] != event.to_dict():
                        return
                elif previous["version"] >= event.version:
                    return
            request = None if complete or event.type == "deadline.cancelled" else event.to_dict()
            rows[event.key] = {"version": event.version, "request": request}
            value = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
            try:
                if entry:
                    self._state.update(INDEX, value, entry.revision)
                else:
                    self._state.create(INDEX, value)
                return
            except StateConflict:
                continue
        raise StateConflict("deadline index remained contended")

    def _restore(self) -> None:
        entry = self._state.get(INDEX)
        if entry is None or entry.revision == self._revision:
            return
        rows = json.loads(entry.value)
        self._pending = [
            (row["request"]["data"]["deadline_ms"], key, row["request"])
            for key, row in rows.items()
            if row["request"] is not None
        ]
        heapq.heapify(self._pending)
        self._revision = entry.revision

    def fire(self, guard: Callable[[], bool] = lambda: True) -> None:
        self._restore()
        while self._pending and self._pending[0][0] <= self._clock():
            if not guard():
                return
            _, _, raw = self._pending[0]
            request = Envelope.from_dict(raw)
            event = deadlines.expired(request)
            self._events.publish(deadlines.subject(event), event)
            self._store(request, complete=True)
            heapq.heappop(self._pending)
