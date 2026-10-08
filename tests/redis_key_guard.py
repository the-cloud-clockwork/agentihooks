"""Keep every test off the production swarm Redis key names.

Imported by the root conftest before any test module, so each swarm store binds the suite's key prefix at import. A
write to a key under a production name is refused and recorded, and the root conftest fails the test that made it.
"""

import os

import redis
from redis.client import Pipeline

from tests.swarm_v2_isolation import RUN_PREFIX

ENV = "AGENTIHOOKS_SWARM_KEY_PREFIX"
PRODUCTION = "agentihooks:"
WRITES = frozenset(
    """
    APPEND BLMOVE BLPOP BRPOP BRPOPLPUSH BZPOPMAX BZPOPMIN COPY DECR DECRBY DEL EXPIRE EXPIREAT GETDEL GETEX GETSET
    HDEL HINCRBY HINCRBYFLOAT HMSET HSET HSETNX INCR INCRBY INCRBYFLOAT LINSERT LMOVE LPOP LPUSH LPUSHX LREM LSET
    LTRIM MSET MSETNX PERSIST PEXPIRE PEXPIREAT PFADD PSETEX PUBLISH RENAME RENAMENX RESTORE RPOP RPOPLPUSH RPUSH
    RPUSHX SADD SET SETBIT SETEX SETNX SETRANGE SMOVE SPOP SREM UNLINK XACK XADD XAUTOCLAIM XCLAIM XDEL XGROUP XTRIM
    ZADD ZINCRBY ZPOPMAX ZPOPMIN ZREM ZREMRANGEBYLEX ZREMRANGEBYRANK ZREMRANGEBYSCORE
    """.split()
)
ALL_KEYS = frozenset(("DEL", "UNLINK"))
PAIRS = frozenset(("MSET", "MSETNX"))
TWO_KEYS = frozenset(("BLMOVE", "BRPOPLPUSH", "COPY", "LMOVE", "RENAME", "RENAMENX", "RPOPLPUSH", "SMOVE"))
BLOCKING = frozenset(("BLPOP", "BRPOP", "BZPOPMAX", "BZPOPMIN"))
written: list[str] = []


class ProductionKey(RuntimeError):
    pass


def _keys(args):
    command = str(args[0]).upper()
    if command in ALL_KEYS:
        return args[1:]
    if command in PAIRS:
        return args[1::2]
    if command in TWO_KEYS:
        return args[1:3]
    if command in BLOCKING:
        return args[1:-1]
    return args[1:2]


def _check(args):
    if not args or str(args[0]).upper() not in WRITES:
        return
    for key in _keys(args):
        text = key.decode(errors="replace") if isinstance(key, bytes) else key
        if isinstance(text, str) and text.startswith(PRODUCTION):
            written.append(text)
            raise ProductionKey(f"a test wrote the production Redis key {text}")


def _guarded(method):
    def guarded(self, *args, **options):
        _check(args)
        return method(self, *args, **options)

    return guarded


os.environ[ENV] = RUN_PREFIX
redis.Redis.execute_command = _guarded(redis.Redis.execute_command)
Pipeline.pipeline_execute_command = _guarded(Pipeline.pipeline_execute_command)
