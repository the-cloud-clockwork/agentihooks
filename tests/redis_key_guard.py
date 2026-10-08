"""Keep every test off the production swarm Redis key names.

Imported by the root conftest before any test module: the key prefix must be set before a swarm store binds it at
import. Any command outside the read list that names a production key is refused and recorded, and the root conftest
fails the test that sent it.
"""

import importlib.util
import os
import sys

from tests.swarm_v2_isolation import RUN_PREFIX

os.environ["AGENTIHOOKS_SWARM_KEY_PREFIX"] = RUN_PREFIX

from scripts.swarm import keyspace  # noqa: E402

PRODUCTION = (f"{keyspace.PRODUCTION}:", "agenticore:")
READS = frozenset(
    """
    BITCOUNT CLIENT DBSIZE DISCARD DUMP ECHO EXEC EXISTS GET GETRANGE HELLO HEXISTS HGET HGETALL HKEYS HLEN HMGET
    HSCAN HSTRLEN HVALS INFO KEYS LINDEX LLEN LRANGE MGET MULTI OBJECT PING PSUBSCRIBE PTTL PUBSUB SCAN SCARD SELECT
    SISMEMBER SMEMBERS SMISMEMBER SSCAN STRLEN SUBSCRIBE TTL TYPE UNWATCH WATCH XINFO XLEN XRANGE XREAD XREVRANGE
    ZCARD ZCOUNT ZMSCORE ZRANGE ZRANGEBYSCORE ZRANK ZREVRANGE ZREVRANGEBYSCORE ZSCAN ZSCORE
    """.split()
)
written: list[str] = []


class ProductionKey(RuntimeError):
    pass


def _check(args):
    if not args or str(args[0]).upper() in READS:
        return
    for arg in args[1:]:
        text = arg.decode(errors="replace") if isinstance(arg, bytes) else arg
        if isinstance(text, str) and text.startswith(PRODUCTION):
            written.append(text)
            raise ProductionKey(f"a test wrote the production Redis key {text}")


def _guarded(method):
    def guarded(self, *args, **options):
        _check(args)
        return method(self, *args, **options)

    return guarded


def _install(client):
    client.Redis.execute_command = _guarded(client.Redis.execute_command)
    for name in ("pipeline_execute_command", "immediate_execute_command"):
        setattr(client.Pipeline, name, _guarded(getattr(client.Pipeline, name)))


class GuardOnImport:
    """Guards redis.client as it is first imported, so test modules that import redis lazily stay lazy."""

    def find_spec(self, name, path, target=None):
        if name != "redis.client":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        run = spec.loader.exec_module

        def exec_module(module):
            run(module)
            _install(module)

        spec.loader.exec_module = exec_module
        return spec


if "redis.client" in sys.modules:
    _install(sys.modules["redis.client"])
else:
    sys.meta_path.insert(0, GuardOnImport())
