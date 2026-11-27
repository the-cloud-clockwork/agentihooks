import json

from scripts.swarm import queue_record
from scripts.swarm.queue_priority import ORDER, apply
from scripts.swarm.store import RedisStore

PENDING = "queue-priority"


def blocking(url: str, journal: queue_record.Journal) -> dict | None:
    return None


def request_held_on_the_head_run(
    store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool
) -> dict:
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


def request_applied_at_once(store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
    journal = queue_record.Journal(url)
    if lift:
        # ok: merge-priority-request-holds-on-head-run
        journal.write("priority-lift", url, "", None, None)
    # ok: merge-priority-request-holds-on-head-run
    return apply(store, slug, url, sender, tasks, journal)
