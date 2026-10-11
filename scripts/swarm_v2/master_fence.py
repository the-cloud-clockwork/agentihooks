"""Owner fenced master writes: only the master seat owning an item answers it, decides it or sets it.

The fence runs inside the ledger's locked write, so the owner check and the write never interleave with another
master's. A question or follow up keeps the seat that decided it until someone other than a master changes it; a task
follows its phase owner.
"""

import functools
import json
import os

from scripts.gates import log
from scripts.gates.base import Who
from scripts.swarm.store import MASTER
from scripts.swarm_v2 import masters

NAME = "master-fence"
SWITCH = "AGENTIHOOKS_MASTER_FENCE"
OFF = "off"
DECISIONS = "master_decisions"
REFUSALS = "write_refusals"
DECIDED = ("questions/", "followups/")
VERBS = {"answer": "answer", "set": "decide", "task_update": "set"}


def decision(op: dict) -> str:
    """The item a covered master write acts on, '' for every other op."""
    kind = op.get("op")
    if kind in ("answer", "task_update"):
        return op["item"]
    if kind != "set":
        return ""
    item = op["path"].rpartition("/")[0]
    return item if item.startswith(DECIDED) else ""


def verb(op: dict) -> str:
    return VERBS[op["op"]]


def refusal(item: str, owner: str, writer: str, action: str, rev: int = 0) -> str:
    if rev:
        return f"{item} was decided by {owner} at revision {rev}, so {writer} cannot {action} it"
    return f"{item} belongs to {owner}, so {writer} cannot {action} it"


def _fields(stamps: dict, item: str) -> dict:
    return {
        path: stamp for path, stamp in stamps.items() if path.startswith(f"{item}/") and not path.endswith("/comments")
    }


def current(record: dict, item: str, stamps: dict) -> bool:
    """A record holds while its own write is still stamped and nobody but a master has written the item since."""
    from scripts.swarm.naming import lane_of

    fields = _fields(stamps, item).values()
    later = [stamp for stamp in fields if stamp["rev"] > record["rev"]]
    return any(stamp["rev"] == record["rev"] for stamp in fields) and all(lane_of(s["by"]) == MASTER for s in later)


def announcement(seat: str, op: dict) -> str:
    if op["op"] == "answer":
        return f"{seat} answered {op['item']}: {op['text'].strip()}"
    if op["op"] == "set":
        return f"{seat} set {op['path']} to {json.dumps(op['value'])}"
    return f"{seat} set {op['item']}: {', '.join(sorted(op['fields']))}"


def measurements(redis, slug: str) -> dict:
    table = masters.MasterSeats(redis)
    return {"master_write_refusals_total": int(table.redis.hget(table.key(slug), REFUSALS) or 0)}


def _connect():
    from scripts.swarm.store import redis_client

    return redis_client()


class Seats:
    """The swarm's live master seats and phase owners, as one write reads them."""

    def __init__(self, slug: str, store, doc: dict) -> None:
        live = [a for a in store.agents(slug) if a.lane == MASTER and a.state != "finished"]
        self.slug, self.doc = slug, doc
        self.names = {a.name: a.seat or a.name for a in live}
        self.live = list(self.names.values())
        self.lead = masters.lead_of(slug, self.live)
        self.table = masters.MasterSeats(store.redis)
        self.count = self.table.count(slug)

    def writer(self, by: str) -> str:
        return self.names.get(by, by)

    def owner(self, item: str, meta: dict) -> tuple[str, int]:
        record = meta.get(DECISIONS, {}).get(item)
        known = [*masters.seats(self.slug, self.count), *self.live]
        if record and record["seat"] in known and current(record, item, meta["stamps"]):
            return record["seat"], record["rev"]
        owners = functools.partial(self.table.owners, self.slug, self.doc)
        return masters.route(item, self.doc, owners, self.live, self.lead), 0


class Fence:
    def __init__(self, slug: str, inner=None, connect=None, home=None) -> None:
        self.slug, self.inner, self.connect, self.home = slug, inner, connect or _connect, home
        self.decided = []
        self._redis = None

    def apply(self, doc, op, ctx, apply):
        from scripts.swarm.naming import lane_of

        item = decision(op)
        if not item or "by" not in op or os.environ.get(SWITCH) == OFF:
            return self._inner(doc, op, ctx, apply)
        by = ctx.author(op["by"])
        if lane_of(by) != MASTER:
            return self._inner(doc, op, ctx, apply)
        try:
            seats = self._seats(doc)
        except Exception as exc:  # a crashed fence lets the write through, counted in the gate log
            reason = f"{type(exc).__name__}: {exc}"
            log.append(self.slug, log.Row.of(NAME, "fail-open", Who(name=by), "ledger", reason), self.home)
            return self._inner(doc, op, ctx, apply)
        if seats is None:
            return self._inner(doc, op, ctx, apply)
        writer = seats.writer(by)
        owner, rev = seats.owner(item, ctx.meta)
        if writer != owner:
            ctx.refused.append(refusal(item, owner, writer, verb(op), rev))
            self._redis.hincrby(seats.table.key(self.slug), REFUSALS)
            return False
        before = _fields(ctx.stamps, item)
        done = self._inner(doc, op, ctx, apply)
        if done and _fields(ctx.stamps, item) != before:
            self._decided(op, item, writer, by, seats.count, ctx)
        return done

    def _decided(self, op, item, writer, by, count, ctx) -> None:
        if item.startswith(DECIDED):
            ctx.meta.setdefault(DECISIONS, {})[item] = {"seat": writer, "rev": ctx.rev}
        if count > 1:
            self.decided.append((op["id"], writer, by, announcement(writer, op), ctx.at / 1000))

    def _seats(self, doc):
        from scripts.swarm.store import RedisStore, SwarmError

        if self._redis is None:
            self._redis = self.connect()
        store = RedisStore(self._redis)
        try:
            store.config(self.slug)
        except SwarmError:
            return None
        return Seats(self.slug, store, doc)

    def _inner(self, doc, op, ctx, apply):
        return self.inner.apply(doc, op, ctx, apply) if self.inner else apply(doc, op, ctx)

    def publish(self, rejected) -> None:
        """Post each admitted decision the ledger kept, after its write committed."""
        from scripts.swarm_v2.masters_channel import MastersChannel

        for op_id, seat, by, text, at in self.decided:
            if op_id not in rejected:
                MastersChannel(self._redis).decision(self.slug, seat, by, text, at)
