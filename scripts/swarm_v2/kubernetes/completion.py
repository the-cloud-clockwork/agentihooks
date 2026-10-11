from collections.abc import Iterable

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.kubernetes.cleanup import Cleanup


class Completion:
    def __init__(self, store: RedisStore, slug: str, cleanup: Cleanup) -> None:
        self.store, self.slug, self.cleanup = store, slug, cleanup

    def __call__(self, pods: Iterable[dict]) -> dict[str, str]:
        return {}
