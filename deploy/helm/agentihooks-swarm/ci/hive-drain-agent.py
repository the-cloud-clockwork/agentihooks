"""Main process of the kind proof worker hive: keeps the hive ready and runs one scripted agent on it.

The first hive pod registers its hive, seats a working agent with a task on it and, once the hive's roll order
reaches that agent's inbox, records the agent's own handoff after a short delay, the way a real agent writes its
document. Every pod records the moment its container is told to stop, which comes only after the chart's preStop
drain returns, so the proof can order the handoff against the stop.
"""

import json
import os
import signal
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from scripts.handoff import envelope
from scripts.hive import registry
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, connect

SLUG = os.environ["DRAIN_SLUG"]
TASK = os.environ["DRAIN_TASK"]
SEAT = f"eng-1@{SLUG}"
DELAY_S = float(os.environ["DRAIN_HANDOFF_DELAY_S"])
DOCUMENT = f"## Done\nWorked {TASK} on the hive.\n\n## Stopped at\nThe hive rolled.\n\n## Next\nContinue {TASK}.\n"


def key(name: str) -> str:
    """The Redis key the proof reads one fixture fact from."""
    return f"kind-drain:{SLUG}:{name}"


def now_ms() -> int:
    """Wall clock milliseconds, the unit every fixture fact carries."""
    return time.time_ns() // 1_000_000


def keep_ready(marker: Path) -> None:
    """Touch the readiness marker the chart's rollout readiness probe reads."""
    while True:
        marker.touch()
        time.sleep(5)


def stopped(store: RedisStore, hive: str) -> None:
    """Record when this container was told to stop, then exit."""
    store.redis.hset(key("stopped"), hive, now_ms())
    sys.exit(0)


def hand_off(store: RedisStore, agent: AgentRecord) -> None:
    """Wait for the roll order in the agent's inbox, then record the agent's own handoff."""
    inbox = InboxStore(store.redis)
    while not inbox.inbox(SEAT):
        time.sleep(1)
    store.redis.set(key("ordered"), now_ms())
    time.sleep(DELAY_S)
    at = now_ms()
    built = envelope.build(store, SLUG, agent, "recycle", None, at)
    store.put_handoff(SLUG, TASK, DOCUMENT, seat=SEAT, envelope=built)
    store.redis.set(key("handed"), json.dumps({"at": at, "hive": agent.hive}))
    store.put_agent(SLUG, replace(agent, state="finished"))


def main() -> None:
    """Keep the hive ready; seat the scripted agent on the first hive only."""
    hive = os.environ["SWARM_HIVE_ID"]
    store = connect()
    for stop in (signal.SIGTERM, signal.SIGINT):
        signal.signal(stop, lambda _signum, _frame: stopped(store, hive))
    registry.update(store.redis, hive, ["ui=no"])
    threading.Thread(target=keep_ready, args=(Path(os.environ["HIVE_READY_FILE"]),), daemon=True).start()
    print(f"hive {hive} serving", flush=True)
    if store.redis.set(key("agent-hive"), hive, nx=True):
        name = store.next_name(SLUG, "eng", now_ms())
        seated = AgentRecord(name, "eng", TASK, seat=SEAT, hive=hive, state="working", started_at=now_ms())
        agent = store.start_execution(SLUG, seated)
        store.redis.set(key("agent"), agent.name)
        print(f"{agent.name} works {TASK} on hive {hive}", flush=True)
        hand_off(store, agent)
        print(f"{agent.name} recorded its handoff", flush=True)
    while True:
        signal.pause()


if __name__ == "__main__":
    main()
