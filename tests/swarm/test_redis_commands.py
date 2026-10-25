import json
import os
import subprocess
import sys

import pytest
import redis

from scripts.swarm import code_reach, redis_commands
from scripts.swarm.keyspace import ROOT
from scripts.swarm.redis_wire import lua, wire

pytestmark = pytest.mark.unit

APP = """
from scripts.lease import acquire
from scripts.store import connect
from scripts.timer import Event, Other


def main():
    from scripts.timer import Timer

    acquire()
    Other()
    with Timer() as timer:
        timer.wait()
        timer.fire()
    Timer.build()
    items[0].zrem("k", "m")
    helper.getrange()
    helper.get("helper")
    shelf.lrem()
    shelf.keys()


def later():
    from scripts.timer import Timer

    t = Timer()
    t.wait()
    o = Other()
    o.blink()
    Event()
    s = connect()
    s.drain()


def admin(client):
    client.acl_setuser("member")


if __name__ == "__main__":
    admin(None)
"""
LEASE = """
EXTEND = "return redis.call('PEXPIRE', KEYS[1], ARGV[1])"


def acquire(redis: "Redis"):
    redis.incr("lease")
    redis.execute_command("XINFO", "STREAM", "lease")
    sub = redis.pubsub()
    sub.subscribe("lease")
"""
TIMER = """
class Timer:
    def __enter__(self):
        self.redis.rpush(self.key, "ready")
        self.close()
        return self

    def close(self):
        self.redis.delete(self.key)

    def wait(self):
        self.redis.blpop(self.key)
        self.listener.psubscribe("timer")

    def __hidden(self):
        self.redis.srem(self.key, "hidden")

    @classmethod
    def build(cls):
        return cls.update()

    @classmethod
    def update(cls):
        return cls.redis.persist("timer")


class Other:
    def fire(self):
        self.redis.lpush(self.key, "fired")

    def lrem(self):
        return None


class Event:
    def wait(self):
        self.redis.lpop(self.key)

    def blink(self):
        self.redis.ltrim(self.key, 0, 1)
"""
STORE = """
class Base:
    def flush(self):
        self.redis.unlink(self.key)


class Store(Base):
    def drain(self):
        self.redis.zadd(self.key, {"m": 1})
        self.flush()


def connect():
    return Store()


def getrange():
    return None
"""
PACKAGE = """
from .sib import two


def start():
    from .rel import one

    one()
    two()
"""
ENTRY = """
from .rel import one
from sib import two
import scripts.pkg.sib as sib_mod
import scripts.pkg.sib


def main():
    one()
    two()


def aliased():
    sib_mod.three()


def dotted():
    scripts.pkg.sib.four()
"""
SIB = """
def two(redis):
    redis.sadd("k", "m")


def three(redis):
    redis.scard("k")


def four(redis):
    redis.hlen("k")
"""
FILES = {
    "scripts/__init__.py": "",
    "scripts/app.py": APP,
    "scripts/lease.py": LEASE,
    "scripts/timer.py": TIMER,
    "scripts/store.py": STORE,
    "scripts/pkg/__init__.py": PACKAGE,
    "scripts/pkg/entry.py": ENTRY,
    "scripts/pkg/rel.py": "def one(redis):\n    redis.hset('k', 'f', 'v')\n",
    "scripts/pkg/sib.py": SIB,
    "scripts/pkg/deep/__init__.py": "",
    "scripts/pkg/deep/leaf.py": "from ..rel import one\n\n\ndef main():\n    one()\n",
}


@pytest.fixture
def tree(tmp_path):
    for name, text in FILES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return tmp_path


@pytest.fixture
def program(tree):
    return code_reach.Program(code_reach.parse(tree))


@pytest.fixture(scope="module")
def published():
    # Built in a subprocess: under the coverage tracer the repository map runs near the hung test dump bound.
    command = [sys.executable, "-m", "scripts.swarm.redis_commands"]
    out = subprocess.run(command, cwd=redis_commands.ROOT, stdout=subprocess.PIPE, text=True, check=True).stdout
    return json.loads(out)["consumers"]


def test_a_redis_py_method_maps_to_the_command_it_sends():
    assert wire(redis.Redis, "incr") == {"INCRBY"}
    assert wire(redis.Redis, "blpop") == {"BLPOP"}
    assert wire(redis.Redis, "zscan_iter") == {"ZSCAN"}
    assert wire(redis.Redis, "pipeline") == frozenset()
    assert wire(redis.Redis, "from_url") == frozenset()


def test_a_method_no_sample_call_completes_is_refused(monkeypatch):
    def unsampled(self, key):
        raise TypeError(key)

    monkeypatch.setattr(redis.Redis, "unsampled", unsampled, raising=False)
    with pytest.raises(LookupError, match="Redis.unsampled"):
        wire(redis.Redis, "unsampled")


def test_lua_scripts_name_the_commands_they_call():
    assert lua("return redis.call('incrby', KEYS[1], 1) + redis.call( \"GET\", KEYS[1])") == {"INCRBY", "GET"}
    assert lua("redis.call('HSET', KEYS[1], 'field', ARGV[1])") == {"HSET"}


def test_only_commands_reached_from_the_entry_point_are_issued(program):
    reached = program.reach(("scripts.app:main",))
    issued = program.commands(reached)
    assert set(issued) == {"INCRBY", "XINFO", "SUBSCRIBE", "PEXPIRE", "RPUSH", "DEL", "BLPOP", "PERSIST"}
    assert issued["INCRBY"] == {"scripts.lease:acquire"}
    assert issued["BLPOP"] == {"scripts.timer:Timer.wait"}
    assert reached["scripts.app:main"] == ""
    assert reached["scripts.timer:Timer.wait"] == "scripts.app:main"
    assert reached["scripts.timer:Timer.__enter__"] == "scripts.timer:Timer"


def test_a_method_read_on_an_object_of_no_known_class_reaches_any_reached_class(program):
    reached = program.reach(("scripts.app:later",))
    assert set(program.commands(reached)) == {"BLPOP", "RPUSH", "DEL", "ZADD", "UNLINK"}
    assert reached["scripts.store:Store.drain"] == "scripts.app:later"


@pytest.mark.parametrize(
    ("entry", "commands"),
    [
        ("scripts.pkg:start", {"HSET", "SADD"}),
        ("scripts.pkg.entry:main", {"HSET", "SADD"}),
        ("scripts.pkg.entry:aliased", {"SCARD"}),
        ("scripts.pkg.entry:dotted", {"HLEN"}),
        ("scripts.pkg.deep.leaf:main", {"HSET"}),
    ],
)
def test_imports_resolve_relative_sibling_aliased_and_dotted(program, entry, commands):
    assert set(program.commands(program.reach((entry,)))) == commands


CYCLE = {
    "scripts/__init__.py": "",
    "scripts/a.py": "from scripts.b import f\n",
    "scripts/b.py": "from scripts.a import f\n",
    "scripts/e.py": "def go(redis: 'Redis'):\n    redis.llen('k')\n",
    "scripts/c.py": """
from redis import Redis

from scripts.a import f


def main():
    import scripts.e

    f()
    scripts.e.go()
    client().hgetall("k")
    client().pipeline().hkeys("k")
    bound(None)


def bound(self):
    self.go()


def client():
    return Redis()
""",
}


def test_cyclic_reexports_nested_dotted_imports_call_receivers_and_unbound_self_resolve(tmp_path):
    (tmp_path / "scripts").mkdir()
    for name, text in CYCLE.items():
        (tmp_path / name).write_text(text)
    program = code_reach.Program(code_reach.parse(tmp_path))
    reached = program.reach(("scripts.c:main",))
    assert program.commands(reached) == {
        "LLEN": {"scripts.e:go"},
        "HGETALL": {"scripts.c:main"},
        "HKEYS": {"scripts.c:main"},
    }
    assert program.units["scripts.c:main"].dotted == {"scripts.e", "scripts.e.go"}
    assert program.units["scripts.c:bound"].reads == {"go"}


def test_a_lock_taken_on_a_client_issues_the_commands_the_lock_sends(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/__init__.py").write_text("")
    (tmp_path / "scripts/guard.py").write_text(
        "def hold(redis: 'Redis'):\n    redis.incr('k')\n    with redis.lock('guard', timeout=1):\n        pass\n"
    )
    program = code_reach.Program(code_reach.parse(tmp_path))
    issued = program.commands(program.reach(("scripts.guard:hold",)))
    assert {"INCRBY", "SET", "EVALSHA", "SCRIPT LOAD", "GET", "DEL", "PEXPIRE"} <= set(issued)
    assert issued["EVALSHA"] == {"scripts.guard:hold"}


SHELF = """
from redis import Redis


def fill():
    items = []
    items.append("k")
    client = Redis.from_url("redis://")
    client.zadd("k", {"m": 1})
    client.pipeline().sadd("s", "m").lpush("l", "v").execute()


def drain(items, sadd):
    items.zrem("k", "m")
    sadd.ltrim("l", 0, 1)
"""


def test_a_list_append_is_not_a_redis_command_while_a_constructed_client_call_still_is(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/__init__.py").write_text("")
    (tmp_path / "scripts/shelf.py").write_text(SHELF)
    program = code_reach.Program(code_reach.parse(tmp_path))
    sender = {"scripts.shelf:fill"}
    assert program.commands(program.reach(("scripts.shelf:fill",))) == dict.fromkeys(
        ("ZADD", "SADD", "LPUSH", "MULTI", "EXEC"), sender
    )
    assert program.commands(program.reach(("scripts.shelf:drain",))) == {}


KINDS = """
import os

from redis import Redis

import scripts.made
from scripts.made import connect


def annotated(redis: "Redis | None", *rows, **opts):
    redis.ping()
    rows.append(1)


def plain(items: list):
    items.append(1)


def other():
    b = connect()
    b.ping()
    kk = connect()


def builtin():
    b = list()
    b.append(1)


def external():
    b = os.getcwd()
    b.append(1)


def nested_external():
    import os as oo

    b = oo.getcwd()
    b.append(1)


def nested_factory():
    from scripts.made import connect as mk

    mk().expire("k", 1)


def unresolved(holder):
    get_redis = holder.redis
    redis = get_redis()
    redis.pttl("k")


def parameter(b):
    alias = b
    alias.append(1)


def unpacked(rows):
    a, b = connect(), []
    a.rpush("l", "v")
    b.append("x")
    (d, (b, e)) = (connect(), ([], 1))
    d.lpop("l")
    first, second = scripts.made.pair()


def bound():
    with connect() as w:
        w.get("k")
    n: Redis = connect()
    n.lrange("l", 0, 1)
    if m := connect():
        m.llen("l")


def boolop(flag, redis):
    client = flag or connect()
    client.hget("k", "f")
    made = connect()
    local = flag and made
    local.hlen("k")
    (flag or redis).hkeys("k")


def ifexp(flag, redis):
    made = connect()
    (made if flag else None).sadd("s", "m")
    (redis if flag else None).sismember("s", "m")
    (connect() if flag else None).srem("s", "m")
    (None if flag else made).zscore("z", "m")
    (None if flag else redis).scard("s")
    (None if flag else connect()).smembers("s")


def piped():
    made = connect()
    batch = made.pipeline()
    batch.incr("n")
    made.pipeline().sadd("s", "m").lpush("l", "v").execute()


def via_unique(holder):
    holder.only_once().ttl("k")
    holder.once_method().exists("k")


def dotted():
    scripts.made.build().strlen("k")


def attributes(h):
    h.conn_x.lindex("l", 0)
    h.alias_x.lset("l", 0, "v")
    h.rows_x.append(1)


def keywords(kwclient, kwalias):
    kwclient.hsetnx("h", "f", "v")
    kwalias.hmget("h", "f")


def rebinding():
    c = connect()
    c.zrem("z", "m")
    c = None


def looped(rows):
    for kk, c in rows:
        kk.lrem("l", 0, "v")


def comprehended(rows):
    return [kk for kk, c in rows if kk.get("type")] + [0 for {}[0] in rows]


def carried():
    prev = None
    conn = None
    for _ in range(2):
        prev = conn
        conn = connect()
    prev.getdel("k")
"""
MADE = """
import os

import redis
from redis import Redis


def connect():
    return Redis()


def build():
    return Redis()


def only_once():
    return Redis()


def pair():
    return Redis(), []


def url():
    r2 = redis.from_url("redis://")
    r2.decrby("n", 1)


def wire_up(thing):
    made = connect()
    thing(kwclient=connect(), kwalias=made)


class Box:
    def make(self):
        return Redis()

    def build(self):
        return None

    def once_method(self):
        return Redis()

    def use(self):
        self.make().hincrby("h", "f", 1)

    def listing(self):
        b = os.getcwd()
        b.append(1)


class Holder:
    def __init__(self):
        made = connect()
        self.conn_x = connect()
        self.alias_x = made
        self.rows_x = []
"""
TYPED = {
    "scripts/__init__.py": "",
    "scripts/aa.py": "from scripts.zz import late\n\n\ndef outer():\n    return late()\n\n\ndef chained():\n    outer().zcard('z')\n",
    "scripts/kinds.py": KINDS,
    "scripts/made.py": MADE,
    "scripts/zz.py": "from redis import Redis\n\n\ndef late():\n    return Redis()\n\n\nclass Other:\n    def make(self):\n        return None\n",
}


@pytest.fixture
def typed(tmp_path):
    for name, text in TYPED.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return code_reach.Program(code_reach.parse(tmp_path))


@pytest.mark.parametrize(
    ("unit", "commands"),
    [
        ("scripts.kinds:annotated", {"PING"}),
        ("scripts.kinds:plain", set()),
        ("scripts.kinds:other", {"PING"}),
        ("scripts.kinds:builtin", set()),
        ("scripts.kinds:external", set()),
        ("scripts.kinds:nested_external", set()),
        ("scripts.kinds:nested_factory", {"EXPIRE"}),
        ("scripts.kinds:unresolved", {"PTTL"}),
        ("scripts.kinds:parameter", set()),
        ("scripts.kinds:unpacked", {"RPUSH", "LPOP"}),
        ("scripts.kinds:bound", {"GET", "LRANGE", "LLEN"}),
        ("scripts.kinds:boolop", {"HGET", "HLEN", "HKEYS"}),
        ("scripts.kinds:ifexp", {"SADD", "SISMEMBER", "SREM", "ZSCORE", "SCARD", "SMEMBERS"}),
        ("scripts.kinds:piped", {"INCRBY", "SADD", "LPUSH", "MULTI", "EXEC"}),
        ("scripts.kinds:via_unique", {"TTL", "EXISTS"}),
        ("scripts.kinds:dotted", {"STRLEN"}),
        ("scripts.kinds:attributes", {"LINDEX", "LSET"}),
        ("scripts.kinds:keywords", {"HSETNX", "HMGET"}),
        ("scripts.kinds:rebinding", {"ZREM"}),
        ("scripts.kinds:looped", {"LREM"}),
        ("scripts.kinds:comprehended", set()),
        ("scripts.kinds:carried", {"GETDEL"}),
        ("scripts.made:url", {"DECRBY"}),
        ("scripts.made:Box.use", {"HINCRBY"}),
        ("scripts.made:Box.listing", set()),
        ("scripts.aa:chained", {"ZCARD"}),
    ],
)
def test_a_receiver_is_a_client_only_when_its_type_traces_to_a_redis_constructor(typed, unit, commands):
    assert set(typed.commands([unit])) == commands


def test_an_entry_point_missing_from_the_code_is_refused(program):
    with pytest.raises(KeyError, match="no function scripts.app:gone, scripts.app:lost under scripts, hooks"):
        program.reach(("scripts.app:main", "scripts.app:gone", "scripts.app:lost"))


def test_scheduler_keys_follow_the_deployment_outside_any_hive(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member")
    assert redis_commands.scheduler_keys("distributed") == {
        "scheduler entries": f"{ROOT}:scheduler:probe:entries",
        "scheduler wake": f"{ROOT}:scheduler:probe:wake:probe",
    }
    assert os.environ["AGENTIHOOKS_HIVE_ID"] == "member"


def test_the_map_lists_each_consumer_role_commands_senders_and_keys(monkeypatch, tree):
    monkeypatch.setattr(redis_commands, "CONSUMERS", {"app": redis_commands.Consumer("worker", ("scripts.app:later",))})
    assert redis_commands.consumer_map("distributed", tree) == {
        "deployment": "distributed",
        "consumers": {
            "app": {
                "role": "worker",
                "commands": {
                    "BLPOP": ["scripts.timer:Timer.wait"],
                    "DEL": ["scripts.timer:Timer.close"],
                    "RPUSH": ["scripts.timer:Timer.__enter__"],
                    "UNLINK": ["scripts.store:Base.flush"],
                    "ZADD": ["scripts.store:Store.drain"],
                },
                "keys": redis_commands.scheduler_keys("distributed"),
            }
        },
    }


def test_every_consumer_but_the_worker_reaches_redis(published):
    assert all(consumer["commands"] for name, consumer in published.items() if name != "worker")


def test_known_real_senders_stay_in_the_published_map(published):
    commands = published["controller"]["commands"]
    assert "scripts.swarm.lease:acquire" in commands["GET"]
    assert "scripts.swarm.execution:ExecutionRegistry.records" in commands["HVALS"]


def test_the_published_map_drops_calls_on_receivers_that_hold_no_redis_client(published):
    senders = {unit for consumer in published.values() for units in consumer["commands"].values() for unit in units}
    excluded = (
        "scripts.swarm_v2.checkpoints.native:",
        "scripts.swarm_v2.herdr.endpoints:",
        "scripts.claude_quota_balancer:",
    )
    assert not {unit for unit in senders if unit.startswith(excluded)}
    assert published["worker"]["commands"] == {}
    assert all("APPEND" not in consumer["commands"] for consumer in published.values())


def test_the_tick_reads_the_swarm_index_before_ticking_each_swarm(published):
    assert "scripts.swarm.store:RedisStore.slugs" in published["tick"]["commands"]["SMEMBERS"]


def test_the_map_names_each_consumer_with_its_redis_role(published):
    roles = {name: consumer["role"] for name, consumer in published.items()}
    assert roles == {
        "controller": "controller",
        "tick": "controller",
        "hub": "controller",
        "ledger server": "ledger",
        "worker": "worker",
        "hooks": "worker",
    }


def test_the_controller_issues_the_lease_counter_and_the_scheduler_wait(published):
    commands = published["controller"]["commands"]
    assert {"INCRBY", "BLPOP"} <= commands.keys()
    assert "scripts.scheduler.process_timer:Timer.wait" in commands["BLPOP"]


def test_the_hub_reads_the_wake_outbox_with_a_command_the_controller_user_grants(published):
    commands = published["hub"]["commands"]
    assert "scripts.hub.inbox_wake:InboxWake.flush" in commands["SMEMBERS"]
    assert "SRANDMEMBER" not in commands


def test_the_controller_scheduler_keys_sit_in_the_swarm_namespace(published):
    assert published["controller"]["keys"] == {
        "scheduler entries": f"{ROOT}:scheduler:probe:entries",
        "scheduler wake": f"{ROOT}:scheduler:probe:wake:probe",
    }


def test_administrator_commands_reach_no_consumer(published):
    for consumer in published.values():
        assert not {command for command in consumer["commands"] if command.startswith(("ACL", "CONFIG"))}


def test_main_prints_the_sorted_map_for_the_distributed_deployment_by_default(monkeypatch, capsys):
    monkeypatch.setattr(redis_commands, "consumer_map", lambda deployment: {"deployment": deployment, "a": 1})
    assert redis_commands.main([]) == 0
    assert capsys.readouterr().out == '{\n "a": 1,\n "deployment": "distributed"\n}\n'
    assert redis_commands.main(["--deployment", "cloud"]) == 0
    assert json.loads(capsys.readouterr().out)["deployment"] == "cloud"
