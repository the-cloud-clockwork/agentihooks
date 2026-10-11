import json

from scripts.swarm.store import RedisStore

ACTIONS = "bottleneck-actions"


def record_action(store: RedisStore, slug: str, stage: str, text: str, at: int) -> None:
    store.redis.hset(store.key(slug, ACTIONS), stage, json.dumps({"text": text, "at": at}))
