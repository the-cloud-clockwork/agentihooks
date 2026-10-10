import argparse
import json
import os
import sys
import time
import uuid
from typing import TYPE_CHECKING

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError, connect
from scripts.swarm_v2.runtime.base import LOCAL as WORKSTATION_BACKEND
from scripts.swarm_v2.runtime.base import SpawnRequest

if TYPE_CHECKING:
    from scripts.swarm_v2.control_service import ControlService

LOCAL, DISTRIBUTED = "local", "distributed"
NO_SPAWNING = "controller spawning is disabled in this deployment mode"
NO_KUBERNETES = "the distributed controller spawns only through its Kubernetes runtime"
WORKSTATION = "the workstation hive spawns {lane} work"


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
    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, runtime, deployment: str):
        self.store, self.slug, self.held = store, slug, held
        self.runtime, self.deployment = runtime, deployment

    def __getattr__(self, name):
        value = getattr(self.runtime, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            lease.renew(self.store, self.slug, self.held)
            return value(*args, **kwargs)

        return call

    def _mode_refusal(self) -> str:
        if self.deployment == LOCAL:
            return ""
        if self.deployment != DISTRIBUTED:
            return NO_SPAWNING
        router = getattr(self.runtime, "router", None)
        return "" if getattr(router, "placement", None) is not None else NO_KUBERNETES

    def placement_refusal(self, config, lane: str, task: dict) -> str:
        if refused := self._mode_refusal():
            return refused
        if self.deployment == LOCAL:
            return ""
        request = SpawnRequest(config, lane, "", task)
        return (
            WORKSTATION.format(lane=lane) if self.runtime.router.spawn_backend(request) == WORKSTATION_BACKEND else ""
        )

    def has_capacity(self, config) -> bool:
        lease.renew(self.store, self.slug, self.held)
        return not self._mode_refusal() and self.runtime.has_capacity(config)

    def spawn(self, config, lane, name, task):
        lease.renew(self.store, self.slug, self.held)
        if refused := self.placement_refusal(config, lane, task):
            raise SwarmError(refused)
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


def run_once(store: RedisStore, ledger=None, runtime=None, messenger=None, runtimes: dict | None = None) -> dict:
    from scripts.swarm.cli import run_tick

    runtimes = runtimes or {}
    return {
        slug: run_tick(store, slug, ledger, runtimes.get(slug, runtime), messenger, scheduled=True)
        for slug in store.slugs()
    }


def tick_once(store: RedisStore, service: "ControlService | None") -> dict:
    if service is None or service.runtime is None:
        return run_once(store)
    return run_once(store, runtimes={service.controller.slug: service.runtime})


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks controller")
    parser.add_argument("action", choices=("run",))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    from scripts import operator_env
    from scripts.swarm import commands
    from scripts.swarm_v2 import control_service

    operator_env.fill(os.environ)
    store = connect()
    service = None
    try:
        service = control_service.host(os.environ, store, commands.hive_id())
        while True:
            for slug, actions in tick_once(store, service).items():
                for action in actions:
                    print(f"{slug}: {action}", flush=True)
            if service is not None:
                service.tick()
            if args.once:
                return 0
            time.sleep(lease.tick_ms() / 1000)
    except SwarmError as exc:
        print(f"controller: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.stop()
