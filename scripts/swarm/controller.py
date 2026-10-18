import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError, connect
from scripts.swarm_v2.runtime.base import LOCAL as WORKSTATION_BACKEND

if TYPE_CHECKING:
    from scripts.swarm_v2.control_service import ControlService

LOCAL, DISTRIBUTED = "local", "distributed"
NO_SPAWNING = "controller spawning is disabled in this deployment mode"
NO_KUBERNETES = "the distributed controller spawns only through its Kubernetes runtime"
WORKSTATION = "the workstation hive spawns {lane} work"
DISABLED = "the {backend} runtime is disabled on this controller"


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
        from scripts.swarm_v2.runtime.routed import RoutedRuntime, hive_controller

        if (
            deployment in ("compose", "hybrid", DISTRIBUTED)
            and isinstance(runtime, RoutedRuntime)
            and callable(getattr(runtime.herdr_runtime, "_profile_decision", None))
        ):
            runtime = hive_controller(runtime, store, slug, held)
        self.runtime, self.deployment = runtime, deployment

    def __getattr__(self, name):
        value = getattr(self.runtime, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            lease.renew(self.store, self.slug, self.held)
            with lease.kept(self.store, self.slug, self.held):
                return value(*args, **kwargs)

        return call

    def _mode_refusal(self) -> str:
        if self.deployment == LOCAL:
            return ""
        router = getattr(self.runtime, "router", None)
        if getattr(getattr(router, "placement", None), "backend", None) == "hive":
            return ""
        if self.deployment != DISTRIBUTED:
            return NO_SPAWNING
        router = getattr(self.runtime, "router", None)
        return "" if getattr(router, "placement", None) is not None else NO_KUBERNETES

    def placement_refusal(self, lane: str, task: dict) -> str:
        if refused := self._mode_refusal():
            return refused
        if self.deployment == LOCAL:
            return ""
        router = self.runtime.router
        if router.placement.backend == "hive":
            if lane not in ("eng", "ci"):
                original = self.runtime.original_runtime.router
                wanted = original.placed_backend(lane, task)
                return (
                    ""
                    if wanted != WORKSTATION_BACKEND and original.enabled_backend(wanted) == wanted
                    else WORKSTATION.format(lane=lane)
                )
            if router.enabled_backend("hive") != "hive":
                return DISABLED.format(backend="hive")
            from scripts.hive.placement import Wait

            selected = self.runtime.hive_launch.select(self.store.config(self.slug), lane, task)
            return selected.reason if isinstance(selected, Wait) else ""
        wanted = router.placed_backend(lane, task)
        if wanted == WORKSTATION_BACKEND:
            return WORKSTATION.format(lane=lane)
        return DISABLED.format(backend=wanted) if router.enabled_backend(wanted) != wanted else ""

    def has_capacity(self, config) -> bool:
        lease.renew(self.store, self.slug, self.held)
        if self._mode_refusal():
            return False
        return self.deployment != LOCAL or self.runtime.has_capacity(config)

    def spawn(self, config, lane, name, task):
        lease.renew(self.store, self.slug, self.held)
        if refused := self.placement_refusal(lane, task):
            raise SwarmError(refused)
        with lease.kept(self.store, self.slug, self.held):
            runtime = self.runtime
            if lane not in ("eng", "ci") and getattr(runtime, "hive_launch", None) is not None:
                runtime = runtime.original_runtime
            return runtime.spawn(config, lane, name, {**task, "controller_epoch": self.held.epoch})


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _host() -> str:
    boot = _read("/proc/sys/kernel/random/boot_id")
    try:
        return f"{boot}:{os.readlink('/proc/self/ns/pid')}" if boot else ""
    except OSError:
        return ""


def _stat(pid) -> list[str]:
    return _read(f"/proc/{pid}/stat").rsplit(")", 1)[-1].split()


def _holder() -> dict:
    host, fields = _host(), _stat(os.getpid())
    return {"host": host, "pid": os.getpid(), "start": fields[19]} if host and len(fields) > 19 else {}


def _gone(pid: int, start: str) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    fields = _stat(pid)
    return len(fields) > 19 and (fields[0] == "Z" or fields[19] != start)


def _dead(raw: str) -> bool:
    held = json.loads(raw) if raw.startswith("{") else {}
    host = _host()
    if not host or held.get("host") != host or not {"pid", "start"} <= held.keys():
        return False
    return _gone(held["pid"], held["start"])


def take_tick_lock(store: RedisStore, slug: str, held: lease.Lease, ttl_ms: int) -> str | None:
    from redis.exceptions import WatchError

    key = store.key(slug, "tick-lock")
    token = json.dumps({"epoch": held.epoch, "token": uuid.uuid4().hex, **_holder()})
    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(key, store.key(slug, "control-owner"))
            lease.require(store, slug, held)
            raw = pipe.get(key)
            if raw and (not raw.startswith("{") or json.loads(raw)["epoch"] == held.epoch) and not _dead(raw):
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
    parser.add_argument("action", choices=("run", "live", "ready"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.action != "run":
        from scripts.swarm import controller_probes

        try:
            store = connect()
        except SwarmError as exc:
            print(f"controller: {exc}")
            return 1
        return getattr(controller_probes, args.action)(os.environ, store)
    from scripts import operator_env
    from scripts.swarm import commands
    from scripts.swarm_v2 import control_service, proof_swarm

    operator_env.fill(os.environ)
    operator_env.classifier(os.environ)
    store = connect()
    service = None
    try:
        proof_swarm.run(os.environ, store)
        service = control_service.host(os.environ, store, commands.hive_id())
        while True:
            for slug, actions in tick_once(store, service).items():
                for action in actions:
                    print(f"{slug}: {action}", flush=True)
            if service is not None:
                service.tick()
            proof_swarm.run(os.environ, store, service.controller.require if service is not None else None)
            if args.once:
                return 0
            time.sleep(lease.tick_ms() / 1000)
    except SwarmError as exc:
        print(f"controller: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.stop()
