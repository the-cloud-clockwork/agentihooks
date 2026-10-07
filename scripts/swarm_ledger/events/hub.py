"""Per ledger event channels: the latest value of each resource, and a bounded log of the patches between them."""

import base64
import binascii
import collections
import secrets
import threading
import time

from . import patch

RETAINED = 256
IDLE_EVICT_S = 300.0


class Expired(Exception):
    pass


class Channel:
    def __init__(self, retained):
        self.epoch = secrets.token_hex(4)
        self.seq = 0
        self.log = collections.deque(maxlen=retained)
        self.resources = {}
        self.subscribers = 0
        self.idle_since = time.monotonic()


def revision(value):
    return ((value or {}).get("_meta") or {}).get("rev", -1)


class Hub:
    def __init__(self, retained=RETAINED, boot=None):
        self.boot = boot or secrets.token_hex(8)
        self.retained = retained
        self.changed = threading.Condition()
        self.channels = {}

    def cursor(self, slug, channel, seq):
        mark = f"{self.boot}:{channel.epoch}:{slug}:{seq}"
        return base64.urlsafe_b64encode(mark.encode()).decode().rstrip("=")

    def position(self, slug, cursor):
        try:
            mark = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
            boot, epoch, owner, seq = mark.split(":")
            seq = int(seq)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise Expired(cursor) from None
        channel = self.channels.get(slug)
        if boot != self.boot or owner != slug or channel is None or epoch != channel.epoch:
            raise Expired(cursor)
        oldest = channel.log[0][0] if channel.log else channel.seq + 1
        if not oldest - 1 <= seq <= channel.seq:
            raise Expired(cursor)
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
            return channel.seq, [
                (channel.seq, self.cursor(slug, channel, channel.seq), "snapshot", dict(channel.resources))
            ]

    def close(self, slug):
        with self.changed:
            channel = self.channels.get(slug)
            if channel:
                channel.subscribers -= 1
                channel.idle_since = time.monotonic()

    def publish(self, slug, name, value):
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
            channel.log.append((channel.seq, self.cursor(slug, channel, channel.seq), name, data))
            self.changed.notify_all()
            return True

    def wait(self, slug, seq, timeout):
        """The events after seq, waiting up to timeout for one; Expired once they fell out of retention."""
        deadline = time.monotonic() + timeout
        with self.changed:
            channel = self.channels[slug]
            while channel.seq == seq:
                left = deadline - time.monotonic()
                if left <= 0:
                    return []
                self.changed.wait(left)
            if channel.log[0][0] > seq + 1:
                raise Expired(self.cursor(slug, channel, seq))
            return [entry for entry in channel.log if entry[0] > seq]

    def evict(self, now=None, idle=IDLE_EVICT_S):
        now = time.monotonic() if now is None else now
        with self.changed:
            for slug in [s for s, c in self.channels.items() if not c.subscribers and now - c.idle_since >= idle]:
                del self.channels[slug]
