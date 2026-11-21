from scripts.swarm import queue_record
from scripts.swarm.queue_priority import apply
from scripts.swarm.store import RedisStore


def request_bad(store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
    journal = queue_record.Journal(url)
    # ruleid: merge-priority-swallowed-lift
    return apply(store, slug, url, sender, tasks, journal)


def request_good(store: RedisStore, slug: str, url: str, sender: str, tasks: list[dict], lift: bool) -> dict:
    journal = queue_record.Journal(url)
    # ok: merge-priority-swallowed-lift
    return apply(store, slug, url, sender, tasks, journal, lift=lift)
