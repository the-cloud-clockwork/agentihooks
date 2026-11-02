"""Per ledger event channels: the latest value of each resource, and a bounded log of the patches between them."""

import collections
import copy
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from . import patch

RETAINED = 256
IDLE_EVICT_S = 300.0


class Expired(Exception):
    pass


class Closed(Exception):
    pass


class Channel:
    idle_since: float

    def __init__(self, retained: int) -> None:
        self.epoch: str = secrets.token_hex()
        self.seq: int = 0
        self.log: collections.deque[tuple[int, str, str, dict[str, Any]]] = collections.deque(maxlen=retained)
        self.resources: dict[str, Any] = {}
        self.subscribers: int = 0


def revision(value: dict[str, Any]) -> int:
    return value["_meta"]["rev"]


class Hub:
    def __init__(self, retained: int = RETAINED, *, clock: Callable[[], float] | None = None) -> None:
        self.retained = retained
        self.clock = time.monotonic if clock is None else clock
        self.changed = threading.Condition()
        self.channels: dict[str, Channel] = {}
        self.closing = False

    @staticmethod
    def cursor(channel: Channel, seq: int) -> str:
        return f"{channel.epoch}.{seq}"

    def position(self, slug: str, cursor: str) -> int:
        """The seq a cursor names; Expired unless it came from this channel's life and its events are still kept."""
        channel = self.channels.get(slug)
        if channel is None or not cursor.startswith(f"{channel.epoch}."):
            raise Expired
        seq = cursor[len(channel.epoch) + 1 :]
        if not (seq.isascii() and seq.isdigit()):
            raise Expired
        seq = int(seq)
        oldest = channel.log[0][0] - 1 if channel.log else channel.seq
        if not oldest <= seq <= channel.seq:
            raise Expired
        return seq

    def has(self, slug: str) -> bool:
        with self.changed:
            return slug in self.channels

    def watched(self) -> list[str]:
        with self.changed:
            return [slug for slug, channel in self.channels.items() if channel.subscribers]

    def resource(self, slug: str, name: str) -> Any | None:
        with self.changed:
            channel = self.channels.get(slug)
            return channel.resources.get(name) if channel else None

    def open(self, slug: str, load: Callable[[], dict[str, Any]], cursor: str | None = None) -> tuple[int, list[Any]]:
        """Subscribe and answer (seq, first events): a snapshot without a cursor, the retained events after one."""
        seq: int | None = None
        with self.changed:
            if cursor is not None:
                seq = self.position(slug, cursor)
            channel = self.channels.setdefault(slug, Channel(self.retained))
            channel.subscribers += 1
            missing = not channel.resources
        if missing:
            try:
                loaded = load()
            except BaseException:
                self.close(slug)
                raise
            with self.changed:
                for name, value in loaded.items():
                    channel.resources.setdefault(name, copy.deepcopy(value))
        with self.changed:
            if cursor is not None:
                assert seq is not None
                return seq, [entry for entry in channel.log if entry[0] > seq]
            return self.snapshot(slug)

    def snapshot(self, slug: str) -> tuple[int, list[Any]]:
        with self.changed:
            channel = self.channels[slug]
            return channel.seq, [(channel.seq, self.cursor(channel, channel.seq), "snapshot", dict(channel.resources))]

    def close(self, slug: str) -> None:
        with self.changed:
            channel = self.channels.get(slug)
            if channel:
                channel.subscribers -= 1
                channel.idle_since = self.clock()

    def publish(self, slug: str, name: str, value: Any) -> bool:
        """Record value as the latest copy of a resource and log the patch from the previous copy."""
        return self._publish(slug, name, value, False)

    def publish_snapshot(self, slug: str, name: str, value: object) -> bool:
        """The finite JSON generation and all its descendants must remain immutable."""
        return self._publish(slug, name, value, True)

    def _publish(self, slug: str, name: str, value: Any, shared: bool) -> bool:
        with self.changed:
            channel = self.channels.get(slug)
            if channel is None:
                return False
            if name not in channel.resources:
                channel.resources[name] = value if shared else copy.deepcopy(value)
                return False
            old = channel.resources[name]
            if name == "ledger" and revision(value) < revision(old):
                return False
            change = patch.diff(old, value, shared=True) if shared else patch.diff(old, value)
            if change is None:
                return False
            if shared:
                channel.resources[name] = value
            else:
                change = copy.deepcopy(change)
                channel.resources[name] = patch.apply(old, change)
            channel.seq += 1
            data = {"patch": change, "rev": revision(value)} if name == "ledger" else {"patch": change}
            channel.log.append((channel.seq, self.cursor(channel, channel.seq), name, data))
            self.changed.notify_all()
            return True

    def wait(self, slug: str, seq: int, timeout: float | None) -> list[Any]:
        """The events after seq, waiting up to timeout for one; Expired once they fell out of retention, Closed once
        the hub drains."""
        with self.changed:
            channel = self.channels[slug]
            self.changed.wait_for(lambda: channel.seq != seq or self.closing, timeout)
            if self.closing:
                raise Closed
            if channel.log and channel.log[0][0] > seq + 1:
                raise Expired
            return [entry for entry in channel.log if entry[0] > seq]

    def drain(self) -> None:
        with self.changed:
            self.closing = True
            self.changed.notify_all()

    def evict(self, now: float | None = None, idle: float = IDLE_EVICT_S) -> None:
        now = self.clock() if now is None else now
        with self.changed:
            for slug in [s for s, c in self.channels.items() if not c.subscribers and now >= c.idle_since + idle]:
                del self.channels[slug]
