import json


def executions(store, slug):
    # ruleid: tick-unbounded-store-read
    return {name: json.loads(raw) for name, raw in store.redis.hgetall(store.key(slug, "executions")).items()}


def piped(pipe, key):
    # ruleid: tick-unbounded-store-read
    return pipe.hgetall(key)


def every_field(redis, key):
    # ruleid: tick-unbounded-store-read
    return dict(redis.hscan_iter(key))


def one_hash_page(redis, key):
    # ruleid: tick-unbounded-store-read
    return redis.hscan(key, 0)


def every_key(redis, slug):
    # ruleid: tick-unbounded-store-read
    return list(redis.scan_iter(match=f"swarm:{slug}:*"))


def key_cursor(redis):
    # ruleid: tick-unbounded-store-read
    return redis.scan(0, match="swarm:*")


def key_pattern(redis, slug):
    # ruleid: tick-unbounded-store-read
    return redis.keys(f"swarm:{slug}:*")


def expiry_elsewhere(redis, key):
    # ruleid: tick-unbounded-store-read
    return redis.hgetall(key)


def sets_expiry(redis, key):
    redis.expire(key, 3600)


def named_fields(store, slug, names):
    # ok: tick-unbounded-store-read
    return store.redis.hmget(store.key(slug, "executions"), names)


def paged_keys(redis, slug):
    # ok: tick-unbounded-store-read
    return list(redis.scan_iter(match=f"swarm:{slug}:*", count=500))


def paged_hash(redis, key, cursor):
    # ok: tick-unbounded-store-read
    return redis.hscan(key, cursor, count=200)


def expiring(redis, key):
    redis.expire(key, 3600)
    # ok: tick-unbounded-store-read
    return redis.hgetall(key)


def expiry_result(pipe, key):
    kept = pipe.pexpire(key, 60_000)
    # ok: tick-unbounded-store-read
    return kept, pipe.hgetall(key)


def archived(store, slug):
    if store.ended(slug):
        store.archive_ended_executions(slug)
    # ok: tick-unbounded-store-read
    return store.redis.hgetall(store.key(slug, "executions"))


def archived_by_function(store, slug):
    archive_ended(store, slug)
    # ok: tick-unbounded-store-read
    return store.redis.hgetall(store.key(slug, "executions"))


def plain_dict(rows):
    # ok: tick-unbounded-store-read
    return sorted(rows.keys())
