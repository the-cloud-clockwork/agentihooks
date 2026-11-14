"""Which operator ledger writes each agent has been shown, shared by the ledger hook, the ledger watch and the inbox.

Whichever path shows a write first marks it; the others skip it, so each write reaches each agent once.
"""

import contextlib
import os

from scripts.inbox.store import MOVE_ATTEMPTS, InboxError, InboxStore, Item, now_ms, owner_key, prefix, redelivery_ms
from scripts.swarm.naming import NameRegistry

TTL_S = 30 * 24 * 3600
CLAIM_BATCH = 5
SEEN_ON_LEDGER = "already shown through the ledger"
UNSETTLED = "returned to pending: its seen mark kept changing"
UNCHECKED = "returned to pending: its seen mark could not be checked"
CLAIM_FAILED = "returned to pending: its claim hit a Redis error before it finished"


def write_ref(slug, event):
    return f"{slug}:{event['rev']}:{event.get('id') or event['kind'] + ' ' + event.get('target', '')}"


class SeenMarks:
    def __init__(self, redis):
        self.redis = redis

    @staticmethod
    def key(name):
        from scripts.hive import scope

        return f"{prefix(scope.member())}:seen:{name}"

    def mark(self, name: str, ref: str) -> bool:
        """True when this call is the first to show the write to name; False while a delivery owner holds name."""
        from redis.exceptions import WatchError

        names = NameRegistry(self.redis)
        for _ in range(MOVE_ATTEMPTS):
            with self.redis.pipeline() as pipe:
                try:
                    before = names.revisions(("alias", name))
                    pipe.watch(*names.watched("alias", name))
                    resolved = NameRegistry(pipe).resolve(name)
                    pipe.watch(owner_key(resolved))
                    added = 0
                    if pipe.get(owner_key(resolved)) is None:
                        pipe.multi()
                        pipe.sadd(self.key(resolved), ref)
                        pipe.expire(self.key(resolved), TTL_S)
                        added, _ = pipe.execute()
                    if names.revisions(("alias", name)) != before:
                        if added:
                            self.redis.srem(self.key(resolved), ref)
                        continue
                    return added == 1
                except WatchError:
                    continue
        raise InboxError(f"the delivery owner of {name} changed meanwhile; run it again")

    def seen(self, name, ref):
        return bool(self.redis.sismember(self.key(NameRegistry(self.redis).resolve(name)), ref))

    def seen_many(self, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        if not pairs:
            return set()
        resolved = NameRegistry(self.redis).resolve_many(list(dict.fromkeys(name for name, _ in pairs)))
        with self.redis.pipeline(transaction=False) as pipe:
            for name, ref in pairs:
                pipe.sismember(self.key(resolved[name]), ref)
            rows = pipe.execute()
        return {pair for pair, shown in zip(pairs, rows) if shown}


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


def claim(store: InboxStore, me: str, limit: int = CLAIM_BATCH, skip: dict[str, int] | None = None) -> list[Item]:
    """Deliver at most limit oldest pending items across me, my seat and aliases, leaving out the ids in skip.

    Close ledger writes already shown; an item whose seen check fails returns to pending and joins skip for one
    redelivery window. A Redis error returns every item this call delivered to pending before it propagates; an item
    whose return fails too stays delivered until the next redelivery.
    """
    from redis.exceptions import RedisError

    skip = {} if skip is None else skip
    now, window = now_ms(), redelivery_ms()
    store.redeliver(now, window)
    for item_id in [item_id for item_id, at in skip.items() if now - at >= window]:
        del skip[item_id]
    marks = SeenMarks(store.redis)
    shown: list[Item] = []
    waiting = [item for item in store.pending_mail(me) if item.id not in skip][:limit]
    for item in filter(None, (store.deliver(item.id, me) for item in waiting)):
        try:
            elsewhere = item.ref and _shown_elsewhere(marks, store, item, me)
        except InboxError:
            store.requeue(item.id, me, UNSETTLED)
            continue
        except RedisError:
            _release(store, me, [*shown, item])
            raise
        except Exception:
            store.requeue(item.id, me, UNCHECKED)
            leave_out(skip, item.id)
            continue
        if elsewhere:
            store.close(item.id, me, "done", SEEN_ON_LEDGER)
        else:
            shown.append(item)
    return shown


def leave_out(skip: dict[str, int], item_id: str) -> None:
    """Leave item_id out of the claims that pass skip until one redelivery window from now."""
    skip[item_id] = now_ms()


def _release(store: InboxStore, me: str, items: list[Item]) -> None:
    from redis.exceptions import RedisError

    for item in items:
        with contextlib.suppress(RedisError, InboxError):
            store.requeue(item.id, me, CLAIM_FAILED)


def _shown_elsewhere(marks, store, item, me):
    return not marks.mark(me, item.ref) and not _delivered_before(store, item.id, me)


def _delivered_before(store, item_id, me):
    return sum(e.get("state") == "delivered" and e.get("by") == me for e in store.history(item_id)) > 1


def receiving_session(environ=None):
    from hooks.context.account_sessions import agent_pid
    from hooks.context.broadcast import get_active_sessions

    env = os.environ if environ is None else environ
    session_id = env.get("CLAUDE_CODE_SESSION_ID", "") or env.get("CODEX_THREAD_ID", "")
    if session_id:
        return session_id
    pid = agent_pid()
    return next((sid for sid, row in get_active_sessions().items() if row.get("pid") == pid), "")
