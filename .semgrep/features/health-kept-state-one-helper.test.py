def read(store, slug):
    # ruleid: feature-health-kept-state-one-helper
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def observe(store, slug, at, rows):
    client = store
    # ruleid: feature-health-kept-state-one-helper
    client.redis.set(store.key(slug, KEY), json.dumps({"at": at, "rows": rows}))


def read(store, slug):
    # ok: feature-health-kept-state-one-helper
    return kept.read(store, slug, KEY)


def observe(store, slug, at, rows):
    # ok: feature-health-kept-state-one-helper
    kept.write(store, slug, KEY, at, rows=rows)


def cached(store, key):
    # ok: feature-health-kept-state-one-helper
    return store.redis.get(key)
