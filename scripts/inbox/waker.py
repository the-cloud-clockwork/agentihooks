"""The inbox waker: a long running Redis subscriber that prompts idle Codex panes of running swarms the moment an
inbox item lands, instead of at the next minute tick. Runs as the agentihooks-inbox-waker systemd user service."""

import os
import time

from scripts.inbox import wake
from scripts.inbox.store import NOTIFY, InboxStore

RECHECK_S = 5.0
RUNNING = "running"


def wake_all(store, inbox, herdr, now_ms, window, quiet=wake.DEFAULT_QUIET_S * 1000):
    actions = []
    for slug in store.slugs():
        if store.config(slug).state != RUNNING:
            continue
        agents = [a for a in store.agents(slug) if a.state != "finished"]
        actions += [f"{slug}: {a}" for a in wake.wake_now(inbox, slug, agents, herdr, now_ms, window, quiet)]
    return actions


def run(store, herdr, now_ms, environ=None):
    from redis.exceptions import RedisError

    env = os.environ if environ is None else environ
    inbox = InboxStore(store.redis)
    pubsub, delay = None, 1.0
    try:
        while True:
            try:
                if pubsub is None:
                    pubsub = store.redis.pubsub()
                    pubsub.subscribe(NOTIFY)
                    recheck = True
                else:
                    recheck = pubsub.get_message(timeout=RECHECK_S) is not None
                if recheck:
                    for action in wake_all(store, inbox, herdr, now_ms(), wake.window_ms(env), wake.quiet_ms(env)):
                        print(action)
                delay = 1.0
            except RedisError:
                if pubsub is not None:
                    pubsub.close()
                    pubsub = None
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
    finally:
        if pubsub is not None:
            pubsub.close()
