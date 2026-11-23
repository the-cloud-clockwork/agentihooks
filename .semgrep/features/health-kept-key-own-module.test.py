from scripts.swarm.health import focus_unclaimed, kept, order_waiting

KEY = "some-check"


def own(store, slug, at, rows):
    # ok: feature-health-kept-key-own-module
    before = kept.read(store, slug, KEY)
    # ok: feature-health-kept-key-own-module
    kept.write(store, slug, KEY, at, rows=rows)
    return before


def anothers(store, slug, at):
    # ruleid: feature-health-kept-key-own-module
    seen = kept.read(store, slug, focus_unclaimed.KEY)
    # ruleid: feature-health-kept-key-own-module
    kept.write(store, slug, "order-waiting", at, orders={})
    return seen


def named(store, slug):
    # ruleid: feature-health-kept-key-own-module
    return store.redis.get(store.key(slug, order_waiting.KEY))


def through_the_check(store, slug):
    # ok: feature-health-kept-key-own-module
    return focus_unclaimed.findings(store, slug)
