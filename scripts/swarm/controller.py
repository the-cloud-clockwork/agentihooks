import argparse
import json
import os
import signal
import sys
import uuid
from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.scheduler.process_timer import wait as scheduled_wait
from scripts.swarm import lease, local_slots, tick_stop
from scripts.swarm.controller_probes import HOST_SWARMS_ENV, ticked
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError, connect
from scripts.swarm_ledger import ledger_link
from scripts.swarm_v2.runtime.base import HIVE_LANES
from scripts.swarm_v2.runtime.base import LOCAL as WORKSTATION_BACKEND

if TYPE_CHECKING:
    from scripts.hive.placement import Wait
    from scripts.swarm_v2.control_service import ControlService

LOCAL, HYBRID, DISTRIBUTED = "local", "hybrid", "distributed"
NO_SPAWNING = "controller spawning is disabled in this deployment mode"
NO_KUBERNETES = "the distributed controller spawns only through its Kubernetes runtime"
WORKSTATION = "the workstation hive spawns {lane} work"
DISABLED = "the {backend} runtime is disabled on this controller"
OVERFLOW = "the local machine takes {lane} work"


def deployment_for(config: SwarmConfig, environ: Mapping[str, str]) -> str:
    machine = environ.get("AGENTIHOOKS_DEPLOYMENT", LOCAL if ledger_link.local_mode(environ) else "compose")
    return HYBRID if machine == LOCAL and config.hive_placement else machine


class FencedLedger:
    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, ledger):
        self.store, self.slug, self.held, self.ledger = store, slug, held, ledger

    def __getattr__(self, name):
        value = getattr(self.ledger, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            lease.renew(self.store, self.slug, self.held)
            with lease.fencing(self._epoch(args[0] if args else kwargs.get("slug", self.slug))):
                return value(*args, **kwargs)

        return call

    def _epoch(self, slug: str) -> int | None:
        if slug == self.slug:
            return self.held.epoch
        owner = lease.current(self.store, slug)
        return owner.epoch if owner else None


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
            runtime.router.placement = replace(
                runtime.router.placement, master_seats=partial(_master_seats, store, slug)
            )
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
        refused = self._refusal(lane, task)
        return "" if _to_workstation(lane, refused) else refused

    def _refusal(self, lane: str, task: dict, claim: str = "") -> str:
        task = _master_task(lane, task)
        if refused := self._mode_refusal():
            return refused
        if self.deployment == LOCAL:
            return self._local_refusal(lane, task, claim)
        router = self.runtime.router
        if router.placement.backend == "hive":
            if not _hive_lane(lane, task):
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
            if not isinstance(selected, Wait):
                return ""
            if lane == "master":
                return f"{WORKSTATION.format(lane=lane)}: {selected.reason}"
            return self._overflow(lane, task, selected, claim)
        wanted = router.placed_backend(lane, task)
        if wanted == WORKSTATION_BACKEND:
            return WORKSTATION.format(lane=lane)
        return DISABLED.format(backend=wanted) if router.enabled_backend(wanted) != wanted else ""

    def _local_refusal(self, lane: str, task: dict, name: str) -> str:
        if lane == "master":
            return ""
        from scripts.hive import admission
        from scripts.hive.placement import LOCAL_FULL
        from scripts.swarm.commands import hive_id

        machine = hive_id()
        maximum = local_slots.maximum(self.store, self.slug, self.store.config(self.slug).local_max_agents)
        taken = (
            local_slots.claim(self.store, machine, self.slug, task["id"], name, maximum)
            if name
            else local_slots.held(self.store, machine, self.slug, task["id"])
        )
        if taken >= maximum:
            return f"{LOCAL_FULL}{taken} of its {maximum} agents"
        if name:
            admission.record(self.store.redis, machine, f"{self.slug}/{name}")
        return ""

    def _overflow(self, lane: str, task: dict, waited: "Wait", claim: str) -> str:
        if self.deployment != HYBRID or task.get("hive") or not _workstation_herdr():
            return waited.reason
        from scripts.hive import admission, headroom, placement_waits, registry
        from scripts.hive.placement import LOCAL_FULL, Choice, hosts, overflow
        from scripts.swarm.commands import hive_id

        machine = hive_id()
        local = registry.show(self.store.redis, machine)
        if local is not None and not hosts(waited.role, local, (lane,)):
            return waited.reason
        running = local_slots.running(self.store, self.slug, task["id"])
        held = local_slots.held(self.store, machine, self.slug, task["id"])
        pending = max(0, admission.pending(self.store.redis, machine) - (held - running))
        now = lease.now_ms(self.store)
        found = admission.room(
            headroom.measure(0, now, previous=headroom.read(self.store.redis, machine, now)), running, pending
        )
        maximum = local_slots.maximum(self.store, self.slug, running + found.room)
        found = admission.capped(found, running, maximum)
        local_max = min(maximum, running + found.room)
        refused = f": {found.reason}: {found.detail}"
        chosen = overflow(waited.role, waited, held, local_max)
        if not isinstance(chosen, Choice):
            return chosen.reason + (refused if LOCAL_FULL in chosen.reason else "")
        if (
            claim
            and (taken := local_slots.claim(self.store, machine, self.slug, task["id"], claim, local_max)) >= local_max
        ):
            return f"{waited.reason}{LOCAL_FULL}{taken} of its {local_max} agents{refused}"
        if claim:
            admission.record(self.store.redis, machine, f"{self.slug}/{claim}")
        self.store.redis.hdel(self.store.key(self.slug, placement_waits.WAITS), task["id"])
        return OVERFLOW.format(lane=lane)

    def has_capacity(self, config) -> bool:
        lease.renew(self.store, self.slug, self.held)
        if self._mode_refusal():
            return False
        return self.deployment != LOCAL or self.runtime.has_capacity(config)

    def spawn(self, config, lane, name, task):
        lease.renew(self.store, self.slug, self.held)
        task = _master_task(lane, task)
        try:
            return self._spawn(config, lane, name, task)
        except BaseException:
            from scripts.hive import admission
            from scripts.swarm.commands import hive_id

            local_slots.release(self.store, hive_id(), self.slug, name)
            admission.release(self.store.redis, hive_id(), f"{self.slug}/{name}")
            raise

    def _spawn(self, config, lane, name, task):
        refused = self._refusal(lane, task, name)
        workstation = _to_workstation(lane, refused)
        from scripts.hive.placement import LOCAL_FULL

        if LOCAL_FULL in refused:
            from scripts.swarm.tick import SpawnError

            raise SpawnError(refused, "unavailable")
        if refused and not workstation:
            raise SwarmError(refused)
        with lease.kept(self.store, self.slug, self.held):
            runtime = self.runtime
            if workstation:
                runtime = runtime.herdr_runtime
            elif not _hive_lane(lane, task) and getattr(runtime, "hive_launch", None) is not None:
                runtime = runtime.original_runtime
            return runtime.spawn(config, lane, name, {**task, "controller_epoch": self.held.epoch})

    def collect(self, config, lane, name, task):
        from scripts.swarm.tick import SpawnError
        from scripts.swarm_v2.runtime.base import SpawnRequest

        lease.renew(self.store, self.slug, self.held)
        request = SpawnRequest(config, lane, name, {**task, "controller_epoch": self.held.epoch})
        with lease.kept(self.store, self.slug, self.held):
            outcome = self.runtime.hive_launch.collect(request)
        if not outcome.ok:
            raise SpawnError(outcome.detail, outcome.status)
        return outcome.value

    def expire(self, config, lane, name, task):
        from scripts.swarm_v2.runtime.base import SpawnRequest

        request = SpawnRequest(config, lane, name, {**task, "controller_epoch": self.held.epoch})
        with lease.kept(self.store, self.slug, self.held):
            return self.runtime.hive_launch.expire(request)


def _hive_lane(lane: str, task: dict) -> bool:
    return lane in HIVE_LANES or lane == "master" or bool(task.get("hive"))


def _master_seats(store: RedisStore, slug: str) -> frozenset[str]:
    from scripts import master_claude_login
    from scripts.inbox.seats import seat_address
    from scripts.swarm_v2 import masters

    seats = (masters.seat(index) for index in range(1, masters.MasterSeats(store.redis).count(slug) + 1))
    return frozenset(
        seat for seat in seats if not master_claude_login.enabled(slug, seat_address(slug, seat), "master", "claude")
    )


def _to_workstation(lane: str, refused: str) -> bool:
    if refused == OVERFLOW.format(lane=lane):
        return True
    fallback_refusal = refused.startswith(WORKSTATION.format(lane=lane)) or refused == DISABLED.format(backend="hive")
    return lane == "master" and fallback_refusal and _workstation_herdr()


def _master_task(lane: str, task: dict) -> dict:
    return {"id": "master", **task} if lane == "master" else task


def _workstation_herdr() -> bool:
    from scripts import herdr_host

    return herdr_host.server_running(dict(os.environ))


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


CONTAINER_MARKS = ("/.dockerenv", "/run/.containerenv")


def _contained() -> bool:
    # Pods built from one image share a machine id across nodes.
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) or any(Path(p).exists() for p in CONTAINER_MARKS)


def _holder() -> dict:
    host, fields, machine = _host(), _stat(os.getpid()), _read("/etc/machine-id")
    if not host or len(fields) <= 19:
        return {}
    return {"host": host, "pid": os.getpid(), "start": fields[19], **({"machine": machine} if machine else {})}


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
    if host and _ended_boot(held, host):
        return True
    if not host or held.get("host") != host or not {"pid", "start"} <= held.keys():
        return False
    return _gone(held["pid"], held["start"])


def _ended_boot(held: dict, host: str) -> bool:
    machine = "" if _contained() else _read("/etc/machine-id")
    boot = str(held.get("host", "")).partition(":")[0]
    return bool(machine) and held.get("machine") == machine and bool(boot) and boot != host.partition(":")[0]


def take_tick_lock(store: RedisStore, slug: str, held: lease.Lease, ttl_ms: int) -> str | None:
    from redis.exceptions import WatchError

    key = store.key(slug, "tick-lock")
    token = json.dumps({"epoch": held.epoch, "token": uuid.uuid4().hex, **_holder()})
    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(key, store.key(slug, "control-owner"))
            lease.require(store, slug, held)
            raw = pipe.get(key)
            if raw and not _dead(raw):
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
        for slug in ticked(store.slugs(), os.environ.get(HOST_SWARMS_ENV, ""))
    }


def tick_once(store: RedisStore, service: "ControlService | None") -> dict:
    if service is None or service.runtime is None:
        return run_once(store)
    return run_once(store, runtimes={service.controller.slug: service.runtime})


def _stopped(*_) -> None:
    # The pod grace period can end before the tick's finally reaches its release.
    tick_stop.STOP.set()
    tick_stop.release_all()
    sys.exit(143)


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
    previous = signal.signal(signal.SIGTERM, _stopped)
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
            scheduled_wait(lease.tick_ms() / 1000)
    except SwarmError as exc:
        print(f"controller: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.stop()
        signal.signal(signal.SIGTERM, previous)
