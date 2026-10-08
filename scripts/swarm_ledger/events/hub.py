"""Per ledger event channels: the latest value of each resource, and a bounded log of the patches between them."""

import collections
import secrets
import threading
import time
from collections.abc import Callable

from . import patch

RETAINED = 256
IDLE_EVICT_S = 300.0


class Expired(Exception):
    pass


class Channel:
    def __init__(self, retained):
        self.epoch = secrets.token_hex()
        self.seq = 0
        self.log = collections.deque(maxlen=retained)
        self.resources = {}
        self.subscribers = 0


def revision(value):
    return value["_meta"]["rev"]


class Hub:
    def __init__(self, retained: int = RETAINED, *, clock: Callable[[], float] | None = None) -> None:
        self.retained = retained
        self.clock = time.monotonic if clock is None else clock
        self.changed = threading.Condition()
        self.channels = {}

    @staticmethod
    def cursor(channel, seq):
        return f"{channel.epoch}.{seq}"

    def position(self, slug, cursor):
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

    def has(self, slug):
        with self.changed:
            return slug in self.channels

    def watched(self):
        with self.changed:
            return [slug for slug, channel in self.channels.items() if channel.subscribers]

    def resource(self, slug, name):
        with self.changed:
            channel = self.channels.get(slug)
            return channel.resources.get(name) if channel else None

    def open(self, slug, load, cursor=None):
        """Subscribe and answer (seq, first events): a snapshot without a cursor, the retained events after one."""
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
                    channel.resources.setdefault(name, value)
        with self.changed:
            if cursor is not None:
                return seq, [entry for entry in channel.log if entry[0] > seq]
            return channel.seq, [(channel.seq, self.cursor(channel, channel.seq), "snapshot", dict(channel.resources))]

    def close(self, slug):
        with self.changed:
            channel = self.channels.get(slug)
            if channel:
                channel.subscribers -= 1
                channel.idle_since = self.clock()

    def publish(self, slug, name, value):
        """Record value as the latest copy of a resource and log the patch from the previous copy."""
        with self.changed:
            channel = self.channels.get(slug)
            if channel is None:
                return False
            if name not in channel.resources:
                channel.resources[name] = value
                return False
            old = channel.resources[name]
            if name == "ledger" and revision(value) < revision(old):
                return False
            change = patch.diff(old, value)
            if change is None:
                return False
            channel.resources[name] = value
            channel.seq += 1
            data = {"patch": change, "rev": revision(value)} if name == "ledger" else {"patch": change}
            channel.log.append((channel.seq, self.cursor(channel, channel.seq), name, data))
            self.changed.notify_all()
            return True

    def wait(self, slug, seq, timeout):
        """The events after seq, waiting up to timeout for one; Expired once they fell out of retention."""
        with self.changed:
            channel = self.channels[slug]
            self.changed.wait_for(lambda: channel.seq != seq, timeout)
            if channel.log and channel.log[0][0] > seq + 1:
                raise Expired
            return [entry for entry in channel.log if entry[0] > seq]

    def evict(self, now=None, idle=IDLE_EVICT_S):
        now = self.clock() if now is None else now
        with self.changed:
            for slug in [s for s, c in self.channels.items() if not c.subscribers and now >= c.idle_since + idle]:
                del self.channels[slug]
