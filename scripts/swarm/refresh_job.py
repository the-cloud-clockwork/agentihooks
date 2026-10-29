"""The GitHub, Langfuse and Grafana reading refreshes as a detached job the tick starts and never waits for."""

import subprocess
import sys
import time
import uuid

from scripts.swarm import snapshot, snapshot_job
from scripts.swarm.store import connect

LOCK_MS = 15 * 60_000
START_EVERY_MS = 5 * 60_000


def now_ms():
    return int(time.time() * 1000)


def lock_key(store, slug):
    return store.key(slug, "refresh-lock")


def started_key(store, slug):
    return store.key(slug, "refresh-started")


def log_path(slug):
    return snapshot.path(slug).parent / "refresh-job.log"


def spawn(slug):
    log = log_path(slug)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as errors:
        subprocess.Popen(
            [sys.executable, "-m", "scripts.swarm.refresh_job", slug],
            stdin=subprocess.DEVNULL,
            stdout=errors,
            stderr=errors,
            start_new_session=True,
        )


def start(store, slug):
    if store.redis.exists(lock_key(store, slug)):
        return False
    if not store.redis.set(started_key(store, slug), 1, nx=True, px=START_EVERY_MS):
        return False
    spawn(slug)
    return True


def refreshes(store, slug, ledger, now_ms):
    from scripts.ci_budget import defects, flakes
    from scripts.swarm import ci_speed, grafana_alerts, langfuse, queue_priority

    config = store.config(slug)
    return [
        lambda: queue_priority.refresh(store, slug, ledger.state(slug)["tasks"], ledger),
        lambda: ci_speed.refresh(slug, config, store, now_ms),
        lambda: defects.refresh(slug, config, store, now_ms),
        lambda: flakes.refresh(slug, config, store, ledger, now_ms),
        lambda: langfuse.refresh(store, slug, now_ms),
        lambda: grafana_alerts.refresh(store.redis, slug, now_ms),
    ]


def run(store, slug, ledger, now_ms):
    """Every due refresh under the swarm's refresh lock; returns their actions, or None when this hive lost the
    swarm's lease or another job holds the lock."""
    from scripts.swarm import commands, lease

    held = lease.current(store, slug)
    if held is None or held.owner != commands.hive_id():
        return None
    key, token = lock_key(store, slug), uuid.uuid4().hex
    if not store.redis.set(key, token, nx=True, px=LOCK_MS):
        return None
    actions = []
    try:
        for refresh in refreshes(store, slug, ledger, now_ms):
            actions += refresh()
        return actions
    finally:
        snapshot_job._release(store, key, token)


def main(argv):
    from scripts.swarm.ledger_client import LedgerClient

    for action in run(connect(), argv[0], LedgerClient(), now_ms()) or []:
        print(f"{argv[0]}: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
