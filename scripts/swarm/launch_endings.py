import json
from typing import TYPE_CHECKING

from scripts.swarm.store import RedisStore

if TYPE_CHECKING:
    from redis import Redis

ENDINGS = "launch-endings"
RECORD_REFUSAL = """
local previous = redis.call('HGET', KEYS[1], ARGV[1])
local ending = cjson.decode(ARGV[2])
ending.diagnostic = not previous or cjson.decode(previous).diagnostic == true
return redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(ending))
"""


def _client(store: RedisStore) -> "Redis":
    return store.redis  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def record_refusal(store: RedisStore, slug: str, execution_id: str, reason: str, generation: int) -> None:
    ending = json.dumps({"reason": reason, "generation": generation})
    client = _client(store)
    client.eval(RECORD_REFUSAL, 1, store.key(slug, ENDINGS), execution_id, ending)


def terminal(store: RedisStore, slug: str, execution_id: str) -> bool:
    client = _client(store)
    raw = client.hget(store.key(slug, ENDINGS), execution_id)
    return raw is not None and json.loads(raw).get("diagnostic") is not True
