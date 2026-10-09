import socket

from scripts.swarm import push

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
    redis.call('RPUSH', KEYS[2], event)
end
return event
"""


def key(kind: str) -> str:
    return f"agentihooks:host:{socket.gethostname()}:incident:{kind}"


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
