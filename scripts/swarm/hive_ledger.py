"""Hive agents' ledger write results, kept in each agent's session readings. The workstation's own ledger probe
cannot see a hosted route that fails only for hives, so repeated gateway errors there open a swarm outage that only a
hive agent's successful write ends."""

KIND = "hive-ledger"
GATEWAY = (502, 503, 504)
THRESHOLD = 3
WINDOW_MS = 10 * 60 * 1000
KEPT = 20


def record(redis, slug, name, code, now_ms):
    pass


def read(redis, slug, name):
    return {}


def report(code, environ=None, redis=None, now_ms=None):
    return False


def observe(store, slug, now_ms):
    pass
