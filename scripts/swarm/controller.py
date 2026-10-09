import argparse
import json
import os
import sys
import time
import uuid

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError, connect


class FencedLedger:
    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, ledger):
        self.store, self.slug, self.held, self.ledger = store, slug, held, ledger

    def __getattr__(self, name):
        value = getattr(self.ledger, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            lease.renew(self.store, self.slug, self.held)
            with lease.fencing(self.held.epoch):
                return value(*args, **kwargs)

        return call


class FencedRuntime:
    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, runtime, spawning: bool):
        self.store, self.slug, self.held = store, slug, held
        self.runtime, self.spawning = runtime, spawning

    def __getattr__(self, name):
        value = getattr(self.runtime, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            lease.renew(self.store, self.slug, self.held)
            return value(*args, **kwargs)

        return call

    def has_capacity(self, config) -> bool:
        lease.renew(self.store, self.slug, self.held)
        return self.spawning and self.runtime.has_capacity(config)

    def spawn(self, config, lane, name, task):
        lease.renew(self.store, self.slug, self.held)
        if not self.spawning:
            raise SwarmError("controller spawning is disabled in this deployment mode")
        return self.runtime.spawn(config, lane, name, {**task, "controller_epoch": self.held.epoch})


def take_tick_lock(store: RedisStore, slug: str, held: lease.Lease, ttl_ms: int) -> str | None:
    from redis.exceptions import WatchError

    key = store.key(slug, "tick-lock")
    token = json.dumps({"epoch": held.epoch, "token": uuid.uuid4().hex})
    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(key, store.key(slug, "control-owner"))
            lease.require(store, slug, held)
            raw = pipe.get(key)
            if raw and (not raw.startswith("{") or json.loads(raw)["epoch"] == held.epoch):
                return None
            pipe.multi()
            pipe.set(key, token, px=ttl_ms)
            pipe.execute()
            return token
        except WatchError:
            return None


def release_tick_lock(store: RedisStore, slug: str, token: str) -> None:
    from redis.exceptions import WatchError

    key = store.key(slug, "tick-lock")
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


def keep_tick(store: RedisStore, slug: str, held: lease.Lease, token: str, ttl_ms: int) -> None:
    from redis.exceptions import WatchError

    lease.renew(store, slug, held)
    key = store.key(slug, "tick-lock")
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                if pipe.get(key) != token:
                    raise SwarmError("the tick lock is stale")
                pipe.multi()
                pipe.pexpire(key, ttl_ms)
                pipe.execute()
                return
            except WatchError:
                continue


def run_once(store: RedisStore, ledger=None, runtime=None, messenger=None) -> dict:
    from scripts.swarm.cli import run_tick

    return {slug: run_tick(store, slug, ledger, runtime, messenger) for slug in store.slugs()}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks controller")
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    from scripts import operator_env

    operator_env.fill(os.environ)
    store = connect()
    try:
        while True:
            for slug, actions in run_once(store).items():
                for action in actions:
                    print(f"{slug}: {action}", flush=True)
            if args.once:
                return 0
            time.sleep(lease.TICK_MS / 1000)
    except SwarmError as exc:
        print(f"controller: {exc}", file=sys.stderr)
        return 1
