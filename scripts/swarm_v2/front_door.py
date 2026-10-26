"""The front door master: the bare master seat alone faces the operator, in page chat and on the claude ai login.

Every other agent reaches the operator only through the front door's inbox. No agent asks the operator first: page
chat takes only the front door's answer to an operator line, and questions, priorities and needs operator follow ups
from agents are refused.
"""

import re

from scripts.gates import log
from scripts.gates.base import Who
from scripts.inbox.seats import SeatRegistry, seat_address
from scripts.swarm.store import MASTER

NAME = "front-door"
CHAT = "chat"
CHAT_WRITES = ("add", "edit", "delete")
NEEDS_OPERATOR = re.compile(r"^followups/[^/]+/needs_operator$")


def seat(slug: str) -> str:
    return seat_address(slug, MASTER)


def holds(slug: str, held: str) -> bool:
    """A master with no seat predates seats and holds the front door."""
    return held in ("", seat(slug))


def chat_write(op: dict) -> bool:
    return op.get("op") in CHAT_WRITES and op.get("thread") == CHAT and "by" in op


def asks_operator(op: dict) -> bool:
    kind = op.get("op")
    if kind == "priority":
        return True
    if kind == "add_item":
        return op.get("list") == "questions" or (op.get("list") == "followups" and bool(op.get("needs_operator")))
    return kind == "set" and op.get("value") is True and bool(NEEDS_OPERATOR.match(str(op.get("path"))))


def route(slug: str) -> str:
    return f'send it to the front door master by inbox with agentihooks msg send {seat(slug)} "<text>"'


def refusal(slug: str, held: str) -> str:
    return f"page chat belongs to the front door master {seat(slug)}, so {held} cannot write it; {route(slug)}"


def say_refusal(slug: str, by: str) -> str:
    return (
        f"no agent writes to the operator first, so {by} cannot say to him; the front door answers him only when "
        f"he writes; {route(slug)}"
    )


def ask_refusal(slug: str, by: str) -> str:
    return (
        f"no agent asks the operator first, so {by} cannot add a question, priority or needs operator follow up; "
        f"the front door answers him only when he writes; {route(slug)}"
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

        if "by" not in op:
            return self._inner(doc, op, ctx, apply)
        by = ctx.author(op["by"])
        lane = lane_of(by)
        if not lane:
            return self._inner(doc, op, ctx, apply)
        if asks_operator(op):
            return self._deny(by, ask_refusal(self.slug, by), ctx)
        if not chat_write(op):
            return self._inner(doc, op, ctx, apply)
        if lane != MASTER:
            return self._deny(by, refusal(self.slug, by), ctx)
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
        return self._deny(by, refusal(self.slug, held), ctx)

    def _deny(self, by, reason, ctx):
        log.append(self.slug, log.Row.of(NAME, "deny", Who(name=by), "ledger", reason), self.home)
        ctx.refused.append(reason)
        return False

    def _inner(self, doc, op, ctx, apply):
        return self.inner.apply(doc, op, ctx, apply) if self.inner else apply(doc, op, ctx)
