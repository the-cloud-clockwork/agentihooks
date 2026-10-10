class ExecutionsAPI:
    def __init__(self, grants, tasks, lease_ms=60_000):
        raise NotImplementedError


def heartbeat_rejections(store, slug):
    raise NotImplementedError


def heartbeat_rejections_total(store, slug):
    raise NotImplementedError
