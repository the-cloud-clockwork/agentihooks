import json

from scripts.swarm import queue_record
from scripts.swarm.queue_priority import ORDER, PENDING, apply, blocking
from scripts.swarm.store import RedisStore


class HeldOnTheHeadRun:
    def request(self, store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
        journal = queue_record.Journal(url)
        # ruleid: merge-priority-request-holds-on-head-run
        held = None if lift else blocking(url, journal)
        if held is not None:
            record = {**held, "sender": sender, "order": store.redis.incr(store.key(slug, ORDER))}
            # ruleid: merge-priority-request-holds-on-head-run
            store.redis.hset(store.key(slug, PENDING), url, json.dumps(record))
            # ruleid: merge-priority-request-holds-on-head-run
            journal.write("priority-deferred", url, "", None, None, **record)
            return {"url": url, "deferred": True, "displaced": [], "errors": [], **held}
        return apply(store, slug, url, sender, tasks, journal)


class AppliedAtOnce:
    def request(self, store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
        journal = queue_record.Journal(url)
        if lift:
            # ok: merge-priority-request-holds-on-head-run
            journal.write("priority-lift", url, "", None, None)
        # ok: merge-priority-request-holds-on-head-run
        return apply(store, slug, url, sender, tasks, journal)


class ReplayedByTheRefresh:
    def pending(self, store: RedisStore, slug: str, url: str, record: dict) -> None:
        journal = queue_record.Journal(url)
        # ok: merge-priority-request-holds-on-head-run
        held = blocking(url, journal, record["group_head"])
        if held is not None:
            # ok: merge-priority-request-holds-on-head-run
            store.redis.hset(store.key(slug, PENDING), url, json.dumps({**record, **held}))
