import socket
import sys

from scripts.inbox.store import InboxStore
from scripts.swarm import push
from scripts.swarm.store import PREFIX

STEP = """
local active = tonumber(redis.call('HGET', KEYS[1], 'active') or '0')
local field = ARGV[1] == '1' and 'bad' or 'good'
local other = ARGV[1] == '1' and 'good' or 'bad'
redis.call('HSET', KEYS[1], other, 0)
local count = redis.call('HINCRBY', KEYS[1], field, 1)
local event = ''
if count >= 2 and ARGV[1] ~= tostring(active) then
    active = tonumber(ARGV[1])
    redis.call('HSET', KEYS[1], 'active', active)
    event = active == 1 and 'raised' or 'resolved'
    if active == 1 then redis.call('HINCRBY', KEYS[1], 'generation', 1) end
    redis.call('RPUSH', KEYS[2], event)
end
return event
"""


MAIL = """
local generation = tonumber(redis.call('HGET', KEYS[1], 'generation') or '0')
local active = redis.call('HGET', KEYS[1], 'active') == '1'
local field = ARGV[1]
if field == 'raised' and not active then generation = generation + 1 end
local value = tostring(generation)
if field == 'resolved' and redis.call('HGET', KEYS[2], 'raised') ~= value then return 0 end
if redis.call('HGET', KEYS[2], field) == value then return 0 end
redis.call('HSET', KEYS[2], field, value)
return 1
"""


def key(kind: str) -> str:
    return f"{PREFIX}:host:{socket.gethostname()}:incident:{kind}"


def step(redis, kind: str, bad: bool) -> str:
    root = key(kind)
    return redis.eval(STEP, 2, root, f"{root}:outbox", int(bad))


def deliver(redis, kind: str, raised: str, resolved: str) -> None:
    root = key(kind)
    lock = redis.lock(f"{root}:delivery", timeout=10, blocking=False)
    if not lock.acquire():
        return
    try:
        event = redis.lindex(f"{root}:outbox", 0)
        if event and push.send("critical" if kind == "ledger" else "alerts", raised if event == "raised" else resolved):
            redis.lpop(f"{root}:outbox")
    finally:
        lock.release()


def mail(redis, kind: str, address: str, text: str, resolved: bool = False) -> bool:
    root = key(kind)
    field = "resolved" if resolved else "raised"
    if not redis.eval(MAIL, 2, root, f"{root}:mail:{address}", field):
        return False
    InboxStore(redis).send("swarm", address, text, fyi=resolved)
    return True


def host_pressure(store, slug: str) -> list[str]:
    from redis.exceptions import RedisError

    from scripts.swarm import host_budget, ledger_probe

    try:
        sample = host_budget.read_host()
        if sample is None:
            return []
        marks = host_budget.thresholds(store.config(slug))
        bad = sample.load1 / sample.cpus > marks.load_high or sample.available_mb < marks.memory_per_agent_mb
        step(store.redis, "pressure", bad)
        raised = "Host pressure: load is above the high mark or memory is below one agent share."
        resolved = "Host pressure resolved: load and available memory are within the host budget."
        deliver(store.redis, "pressure", raised, resolved)
        active = store.redis.hget(key("pressure"), "active") == "1"
        if mail(
            store.redis,
            "pressure",
            ledger_probe.master_address(store, slug),
            raised if active else resolved,
            not active,
        ):
            return ["raised the host pressure alert" if active else "cleared the host pressure alert"]
        return []
    except (OSError, RedisError):
        print("host pressure check unavailable", file=sys.stderr)
        return []
