"""Which operator ledger writes each agent has been shown, shared by the ledger hook, the ledger watch and the inbox.

Whichever path shows a write first marks it; the others skip it, so each write reaches each agent once.
"""

import os

from scripts.inbox.store import now_ms, redelivery_ms
from scripts.swarm.keyspace import ROOT

PREFIX = f"{ROOT}:inbox:seen"
TTL_S = 30 * 24 * 3600
SEEN_ON_LEDGER = "already shown through the ledger"


def write_ref(slug, event):
    return f"{slug}:{event['rev']}:{event.get('id') or event['kind'] + ' ' + event.get('target', '')}"


class SeenMarks:
    def __init__(self, redis):
        self.redis = redis

    def key(self, name):
        return f"{PREFIX}:{name}"

    def mark(self, name, ref):
        """True when this call is the first to show the write to name."""
        with self.redis.pipeline() as pipe:
            pipe.sadd(self.key(name), ref)
            pipe.expire(self.key(name), TTL_S)
            added, _ = pipe.execute()
        return added == 1

    def seen(self, name, ref):
        return bool(self.redis.sismember(self.key(name), ref))


def marks_for(slug, environ=None):
    """The marks store for a session working this swarm ledger, None for any other session or without Redis."""
    env = os.environ if environ is None else environ
    if not slug or env.get("AGENTIHOOKS_SWARM") != slug:
        return None
    try:
        from scripts.swarm.store import redis_client

        return SeenMarks(redis_client(env))
    except Exception:
        return None


def first_showing(marks, name, slug, events):
    """The events not yet shown to name, now marked shown; every event when the marks are unreachable."""
    if marks is None:
        return list(events)
    try:
        return [event for event in events if marks.mark(name, write_ref(slug, event))]
    except Exception:
        return list(events)


def claim(store, me):
    """Deliver each item pending for me, its seat and aliases included; a ledger write already shown closes instead."""
    store.redeliver(now_ms(), redelivery_ms())
    marks = SeenMarks(store.redis)
    shown = []
    for item in filter(None, (store.deliver(item.id, me) for item in store.pending_mail(me))):
        if item.ref and not marks.mark(me, item.ref) and not _delivered_before(store, item.id, me):
            store.close(item.id, me, "done", SEEN_ON_LEDGER)
        else:
            shown.append(item)
    return shown


def _delivered_before(store, item_id, me):
    return any(e.get("state") == "delivered" and e.get("by") == me for e in store.history(item_id)[:-1])
