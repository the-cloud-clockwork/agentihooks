"""The automatic snapshot as a detached job the tick starts and never waits for, one job per swarm under its lock."""

import os
import subprocess
import sys
import time
import uuid

from scripts.swarm import snapshot
from scripts.swarm.store import connect

LOCK_MS = 30 * 60_000


def now_ms():
    return int(time.time() * 1000)


def lock_key(store, slug):
    return store.key(slug, "snapshot-lock")


def log_path(slug):
    return snapshot.path(slug).parent / "snapshot-job.log"


def spawn(slug):
    log = log_path(slug)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as errors:
        subprocess.Popen(
            [sys.executable, "-m", "scripts.swarm.snapshot_job", slug],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            start_new_session=True,
        )


def start(store, slug, now_ms, environ):
    """Launch the job when a snapshot is due and no job holds the lock; never waits for it."""
    if not snapshot.due(store, slug, now_ms, environ) or store.redis.exists(lock_key(store, slug)):
        return False
    spawn(slug)
    return True


def run(store, slug, now_ms, environ, run=subprocess.run):
    """Take the due snapshot under the swarm's snapshot lock; returns its file, or None when held or not due."""
    key, token = lock_key(store, slug), uuid.uuid4().hex
    if not store.redis.set(key, token, nx=True, px=LOCK_MS):
        return None
    try:
        return snapshot.auto(store, slug, now_ms, environ, run)
    finally:
        _release(store, key, token)


def _release(store, key, token):
    from redis.exceptions import WatchError

    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(key)
            if pipe.get(key) != token:
                return
            pipe.multi()
            pipe.delete(key)
            pipe.execute()
        except WatchError:
            return


def main(argv):
    run(connect(), argv[0], now_ms(), os.environ)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
