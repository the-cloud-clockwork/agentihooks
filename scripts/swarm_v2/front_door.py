"""The front door master: the bare master seat alone faces the operator, in page chat and on the claude ai login.

Backend master seats reach the operator only through the front door, over the masters channel.
"""

from scripts.gates import log
from scripts.gates.base import Who
from scripts.inbox.seats import SeatRegistry, seat_address
from scripts.swarm.store import MASTER

NAME = "front-door"
CHAT = "chat"
CHAT_WRITES = ("add", "edit", "delete")


def seat(slug: str) -> str:
    return seat_address(slug, MASTER)


def holds(slug: str, held: str) -> bool:
    """A master with no seat predates seats and holds the front door."""
    return held in ("", seat(slug))


def chat_write(op: dict) -> bool:
    return op.get("op") in CHAT_WRITES and op.get("thread") == CHAT and "by" in op


def refusal(slug: str, held: str) -> str:
    return (
        f"page chat belongs to the front door master {seat(slug)}, so {held} cannot write it; ask the front door "
        f'with agentihooks swarm {slug} masters-channel say "<text>"'
    )


def _connect():
    from scripts.swarm.store import redis_client

    return redis_client()


class Door:
    def __init__(self, slug: str, inner=None, connect=None, home=None) -> None:
        self.slug, self.inner, self.connect, self.home = slug, inner, connect or _connect, home
        self._redis = None

    def apply(self, doc: dict, op: dict, ctx, apply) -> bool:
        from scripts.swarm.naming import lane_of

        if not chat_write(op):
            return self._inner(doc, op, ctx, apply)
        by = ctx.author(op["by"])
        if lane_of(by) != MASTER:
            return self._inner(doc, op, ctx, apply)
        try:
            if self._redis is None:
                self._redis = self.connect()
            held = SeatRegistry(self._redis).known_seat(by)
        except Exception as exc:  # a crashed door lets the write through, counted in the gate log
            reason = f"{type(exc).__name__}: {exc}"
            log.append(self.slug, log.Row.of(NAME, "fail-open", Who(name=by), "ledger", reason), self.home)
            return self._inner(doc, op, ctx, apply)
        if holds(self.slug, held):
            return self._inner(doc, op, ctx, apply)
        reason = refusal(self.slug, held)
        log.append(self.slug, log.Row.of(NAME, "deny", Who(name=by), "ledger", reason), self.home)
        ctx.refused.append(reason)
        return False

    def _inner(self, doc, op, ctx, apply):
        return self.inner.apply(doc, op, ctx, apply) if self.inner else apply(doc, op, ctx)
