import argparse
import json
import os
import signal
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import partial
from pathlib import Path
from time import monotonic, time
from types import ModuleType
from typing import TYPE_CHECKING, Any, NoReturn

from scripts.events import nkey
from scripts.events.config import BACKEND_ENV, BusConfig
from scripts.events.factory import open_bus
from scripts.gates import intent
from scripts.scheduler.process_timer import wait as scheduled_wait
from scripts.swarm import holder_pod, lease, local_slots, redis_grace, replica, tick_stop, timing
from scripts.swarm.controller_probes import HOST_SWARMS_ENV, ticked
from scripts.swarm.ledger_client import LedgerClient, LedgerGone, LedgerUnreachable
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError, Unreachable, connect, store_errors
from scripts.swarm_ledger import ledger_link
from scripts.swarm_v2.runtime.base import HIVE_LANES
from scripts.swarm_v2.runtime.base import LOCAL as WORKSTATION_BACKEND
from scripts.tick.adapters.processes import PassProcesses
from scripts.tick.ports.tools import PASS_TOOLS

if TYPE_CHECKING:
    from scripts.hive.admission import Room
    from scripts.hive.launch import Selection
    from scripts.hive.placement import Wait
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.control_service import ControlService

LOCAL, HYBRID, DISTRIBUTED = "local", "hybrid", "distributed"
POD_NAME = "AGENTIHOOKS_POD_NAME"
NO_SPAWNING = "controller spawning is disabled in this deployment mode"
NO_KUBERNETES = "the distributed controller spawns only through its Kubernetes runtime"
WORKSTATION = "the workstation hive spawns {lane} work"
DISABLED = "the {backend} runtime is disabled on this controller"
OVERFLOW = "the local machine takes {lane} work"
AGENT_CHANGES = frozenset(["retire", "reap_name", "close_space", "close_pane", "resume"])


def deployment_for(config: SwarmConfig, environ: Mapping[str, str]) -> str:
    machine = environ.get("AGENTIHOOKS_DEPLOYMENT", LOCAL if ledger_link.local_mode(environ) else "compose")
    return HYBRID if machine == LOCAL and config.hive_placement else machine


class FencedRuntime:
    def __init__(self, store: RedisStore, slug: str, held: lease.Lease, runtime, deployment: str):
        self.store, self.slug, self.held = store, slug, held
        self._local_slots_cache: local_slots.PassCache | None = None
        self.kubernetes_proof = (
            slug.startswith("proof-") and os.environ.get("AGENTIHOOKS_CONTROL_KUBERNETES_PROOF") == slug
        )
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

    def __getattr__(self, name: str) -> Any:
        found = getattr(self.runtime, name)
        return partial(self._changing, found) if name in AGENT_CHANGES else found

    def live_names(self) -> set[str]:
        """The live agent names, listed once per tick pass and again after a fenced runtime changes its agents."""
        shared = _pass_processes()
        return self.runtime.live_names() if shared is None else shared.live_names(self.runtime.live_names)

    def _changing(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        finally:
            if (shared := _pass_processes()) is not None:
                shared.forget_names()

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

    def begin_placement_pass(self) -> None:
        if launch := getattr(self.runtime, "hive_launch", None):
            launch.placement_cache = {}
            launch.herdr.pass_memo = {}
        self._local_slots_cache = local_slots.PassCache()

    def end_placement_pass(self) -> None:
        if launch := getattr(self.runtime, "hive_launch", None):
            launch.placement_cache = None
            launch.herdr.pass_memo = None
        self._local_slots_cache = None

    def placement_refusal(self, lane: str, task: dict) -> str:
        refused = self._refusal(lane, task)
        return "" if _to_workstation(lane, refused, self._pass_memo()) else refused

    def _pass_memo(self) -> dict | None:
        launch = getattr(self.runtime, "hive_launch", None)
        return getattr(launch.herdr, "pass_memo", None) if launch else None

    def _refusal(self, lane: str, task: dict, claim: str = "") -> str:
        task = _master_task(lane, task)
        if refused := self._mode_refusal():
            return refused
        if self.deployment == LOCAL:
            return self._local_refusal(lane, task, claim)
        router = self.runtime.router
        if router.placement.backend == "hive":
            if not _hive_lane(lane, task, self.kubernetes_proof):
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
                return self._workstation_claim(lane, task, claim, selected)
            if lane == "master":
                return f"{WORKSTATION.format(lane=lane)}: {selected.reason}"
            return self._overflow(lane, task, selected, claim)
        wanted = router.placed_backend(lane, task)
        if wanted == WORKSTATION_BACKEND:
            return WORKSTATION.format(lane=lane)
        return DISABLED.format(backend=wanted) if router.enabled_backend(wanted) != wanted else ""

    def _workstation_claim(self, lane: str, task: dict[str, Any], claim: str, selected: "Selection") -> str:
        from scripts.swarm.commands import hive_id

        if self.runtime.router.placed_backend(lane, task) != WORKSTATION_BACKEND or selected.hive != hive_id():
            return ""
        return self._local_refusal(lane, task, claim)

    def _local_refusal(self, lane: str, task: dict[str, Any], name: str) -> str:
        if lane == "master":
            return ""
        from scripts.hive import admission, placement_waits, registry
        from scripts.hive.placement import LOCAL_FULL, equipped, needs_refusal
        from scripts.swarm.commands import hive_id

        machine = hive_id()
        local = registry.show(self.store.redis, machine)
        if not equipped(task, local):
            return needs_refusal(task, [local] if local else [])
        found, held, maximum = self._workstation_room(machine, task)
        cache = self._local_slots_cache
        taken = local_slots.claim(self.store, machine, self.slug, task["id"], name, maximum, cache) if name else held
        if taken >= maximum:
            self._local_full(machine, taken, maximum)
            refused = f"{LOCAL_FULL}{taken} of its {maximum} agents: {found.reason}: {found.detail}"
            placement_waits.observe(self.store, self.slug, lane, task["id"], refused, lease.now_ms(self.store))
            return refused
        if name:
            admission.record(self.store.redis, machine, f"{self.slug}/{name}")
        return ""

    def _workstation_room(self, machine: str, task: dict) -> tuple["Room", int, int]:
        from scripts.hive import admission, ceiling, registry

        hive = registry.show(self.store.redis, machine)
        cache = self._local_slots_cache
        running = local_slots.running(self.store, self.slug, task["id"], cache)
        held = local_slots.held(self.store, machine, self.slug, task["id"], cache)
        pending = max(0, admission.pending(self.store.redis, machine) - (held - running))
        reading = (
            _host_reading(self.store, machine) if cache is None else cache.once(_host_reading, self.store, machine)
        )
        top = ceiling.ceiling(hive["max_agents"], reading) if hive is not None else None
        found = admission.room(reading, running, pending, top)
        maximum = local_slots.maximum(self.store, self.slug, running + found.room)
        found = admission.capped(found, running, maximum)
        return found, held, min(maximum, running + found.room)

    def _overflow(self, lane: str, task: dict, waited: "Wait", claim: str) -> str:
        if self.deployment != HYBRID or task.get("hive") or not _workstation_herdr(self._pass_memo()):
            return waited.reason
        from scripts.hive import admission, placement_waits, registry
        from scripts.hive.placement import LOCAL_FULL, Choice, equipped, hosts, local_full, overflow
        from scripts.swarm.commands import hive_id

        machine = hive_id()
        local = registry.show(self.store.redis, machine)
        if (local is not None and not hosts(waited.role, local, (lane,))) or not equipped(task, local):
            return waited.reason
        found, held, local_max = self._workstation_room(machine, task)
        refused = f": {found.reason}: {found.detail}"
        reserved = (
            0
            if task.get("workstation")
            else placement_waits.workstation_waiting(self.store, machine, lease.now_ms(self.store))
        )
        chosen = overflow(waited.role, waited, held, local_max, reserved)
        if not isinstance(chosen, Choice):
            if LOCAL_FULL not in chosen.reason:
                return chosen.reason
            self._local_full(machine, held, local_max)
            return chosen.reason + refused
        room = local_max - reserved
        if claim and (taken := local_slots.claim(self.store, machine, self.slug, task["id"], claim, room)) >= room:
            self._local_full(machine, taken, local_max)
            return local_full(waited, taken, local_max, reserved).reason + refused
        if claim:
            admission.record(self.store.redis, machine, f"{self.slug}/{claim}")
        self.store.redis.hdel(self.store.key(self.slug, placement_waits.WAITS), task["id"])
        return OVERFLOW.format(lane=lane)

    def _local_full(self, machine: str, taken: int, maximum: int) -> None:
        from scripts.swarm.health import spawn_stall

        spawn_stall.refuse_local(self.store, self.slug, machine, taken, maximum, lease.now_ms(self.store))

    def has_capacity(self, config) -> bool:
        if self._mode_refusal():
            return False
        return self.deployment != LOCAL or self.runtime.has_capacity(config)

    def launch_room(self, config) -> int:
        room = getattr(self.runtime, "launch_room", None)
        return room(config) if room else 1

    def spawn(self, config: SwarmConfig, lane: str, name: str, task: dict[str, Any]) -> "Placed":
        lease.require_epoch(self.store, self.slug, self.held.epoch)
        task = _master_task(lane, task)
        try:
            return self._changing(self._spawn, config, lane, name, task)
        except BaseException:
            from scripts.hive import admission
            from scripts.swarm.commands import hive_id

            local_slots.release(self.store, hive_id(), self.slug, name)
            admission.release(self.store.redis, hive_id(), f"{self.slug}/{name}")
            raise

    def _spawn(self, config: SwarmConfig, lane: str, name: str, task: dict[str, Any]) -> "Placed":
        with timing.step(f"{__name__}.FencedRuntime._refusal", report=False):
            refused = self._refusal(lane, task, name)
        workstation = _to_workstation(lane, refused, self._pass_memo())
        from scripts.hive.placement import LOCAL_FULL

        if LOCAL_FULL in refused:
            from scripts.swarm.tick import SpawnError

            raise SpawnError(refused, "unavailable")
        if refused and not workstation:
            raise SwarmError(refused)
        from scripts.hive import placement_waits

        runtime = self.runtime
        hive = getattr(runtime, "hive_launch", None) is not None
        counted = workstation or (self.deployment == LOCAL and not hive)
        totals = self.store.key(self.slug, placement_waits.TOTALS) if counted else ""
        if workstation or self.deployment == LOCAL:
            from scripts.hive import admission
            from scripts.swarm.commands import hive_id

            admission.record(self.store.redis, hive_id(), f"{self.slug}/{name}", totals)
        if workstation:
            runtime = runtime.herdr_runtime
        elif not _hive_lane(lane, task, self.kubernetes_proof) and hive:
            runtime = runtime.original_runtime
        else:
            task = self._hive_task(config, lane, task)
        with timing.step(f"{__name__}.FencedRuntime.runtime_spawn", report=False):
            try:
                return runtime.spawn(config, lane, name, {**task, "controller_epoch": self.held.epoch})
            except BaseException:
                if totals:
                    placement_waits.admitted(self.store, self.slug, -1)
                raise

    def _hive_task(self, config: SwarmConfig, lane: str, task: dict[str, Any]) -> dict[str, Any]:
        """A workstation task placed on a workstation hive other than this machine launches there, pinned to it."""
        from scripts.hive.launch import Selection
        from scripts.swarm.commands import hive_id

        launch = getattr(self.runtime, "hive_launch", None)
        if not task.get("workstation") or launch is None:
            return task
        selected = launch.select(config, lane, task)
        if not isinstance(selected, Selection) or selected.hive == hive_id():
            return task
        return {**task, "workstation": False, "hive": selected.hive}


def _host_reading(store: RedisStore, machine: str) -> dict[str, Any] | None:
    """The machine's host reading for workstation room, taken against its last saved reading."""
    from scripts.hive import headroom

    now = lease.now_ms(store)
    reading: dict[str, Any] | None = headroom.measure(0, now, previous=headroom.read(store.redis, machine, now))
    return reading


def _pass_processes() -> PassProcesses | None:
    """The running tick pass's process model, which holds its shared live names listing."""
    processes = getattr(PASS_TOOLS.get(), "processes", None)
    return processes if isinstance(processes, PassProcesses) else None


def _hive_lane(lane: str, task: dict, kubernetes_proof: bool) -> bool:
    if kubernetes_proof and lane == "eng":
        return False
    return lane in HIVE_LANES or lane == "master" or bool(task.get("hive"))


def _master_seats(store: RedisStore, slug: str) -> frozenset[str]:
    """The master seats placed on hives. The claude ai login front door stays local only where local is the
    workstation; a controller in a pod has no workstation checkout, so every seat goes to hive placement there."""
    from scripts import master_claude_login
    from scripts.inbox.seats import seat_address
    from scripts.swarm_v2 import masters

    seats = (masters.seat(index) for index in range(1, masters.MasterSeats(store.redis).count(slug) + 1))
    if os.environ.get(POD_NAME):
        return frozenset(seats)
    return frozenset(
        seat for seat in seats if not master_claude_login.enabled(slug, seat_address(slug, seat), "master", "claude")
    )


def _to_workstation(lane: str, refused: str, memo: dict | None = None) -> bool:
    if refused == OVERFLOW.format(lane=lane):
        return True
    fallback_refusal = refused.startswith(WORKSTATION.format(lane=lane)) or refused == DISABLED.format(backend="hive")
    return lane == "master" and fallback_refusal and _workstation_herdr(memo)


def _master_task(lane: str, task: dict) -> dict:
    return {"id": "master", **task} if lane == "master" else task


def _workstation_herdr(memo: dict | None = None) -> bool:
    from scripts import herdr_host
    from scripts.swarm import timing

    if memo is None:
        return timing.call(herdr_host.server_running, dict(os.environ))
    if "workstation_herdr" not in memo:
        memo["workstation_herdr"] = timing.call(herdr_host.server_running, dict(os.environ))
    return memo["workstation_herdr"]


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
_LIVE: set[str] = set()


def _contained() -> bool:
    # Pods built from one image share a machine id across nodes.
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) or any(Path(p).exists() for p in CONTAINER_MARKS)


def _holder() -> dict:
    host, fields, machine = _host(), _stat(os.getpid()), _read("/etc/machine-id")
    if not host or len(fields) <= 19:
        return {}
    pod = holder_pod.identity(os.environ)
    return {
        "host": host,
        "pid": os.getpid(),
        "start": fields[19],
        **({"machine": machine} if machine else {}),
        **({"pod": pod} if pod else {}),
    }


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
    if host and held.get("host") != host:
        return holder_pod.gone(held.get("pod"), holder_pod.pod(os.environ))
    if not host or "pid" not in held or "start" not in held:
        return False
    return _gone(held["pid"], held["start"]) or _forgotten(raw, held)


def _forgotten(raw: str, held: Any) -> bool:
    """Tell whether ``held`` names this very process while no pass in it still holds the token ``raw``."""
    return held["pid"] == os.getpid() and _stat(os.getpid())[19:20] == [held["start"]] and raw not in _LIVE


def _ended_boot(held: dict, host: str) -> bool:
    machine = "" if _contained() else _read("/etc/machine-id")
    boot = str(held.get("host", "")).partition(":")[0]
    return bool(machine) and held.get("machine") == machine and bool(boot) and boot != host.partition(":")[0]


def _lock_raw(pipe: Any, key: str) -> str | None:
    """Read the tick lock value through a watched pipeline."""
    return pipe.get(key)


def take_tick_lock(store: RedisStore, slug: str, held: lease.Lease, ttl_ms: int, caller: str = "unnamed") -> str | None:
    from redis.exceptions import WatchError

    key = store.key(slug, "tick-lock")
    token = json.dumps({"epoch": held.epoch, "token": uuid.uuid4().hex, **_holder()})
    with store.redis.pipeline() as pipe:
        pipe.watch(key)
        lease.require(store, slug, held)
        raw = _lock_raw(pipe, key)
        if raw and not _dead(raw):
            _journal_lock(slug, caller, "refused", raw)
            return None
        pipe.multi()
        pipe.set(key, token, px=ttl_ms)
        try:
            pipe.execute()
        except WatchError:
            _journal_lock(slug, caller, "refused", raw)
            return None
        _LIVE.add(token)
        _journal_lock(slug, caller, "replaced a dead holder" if raw else "taken", raw)
        return token


def release_tick_lock(store: RedisStore, slug: str, token: str, caller: str = "unnamed") -> None:
    from redis.exceptions import WatchError

    key = store.key(slug, "tick-lock")
    _LIVE.discard(token)
    with store.redis.pipeline() as pipe:
        pipe.watch(key)
        raw = _lock_raw(pipe, key)
        if raw != token:
            _journal_lock(slug, caller, "found another holder", raw)
            return
        pipe.multi()
        pipe.delete(key)
        try:
            pipe.execute()
        except WatchError:
            _journal_lock(slug, caller, "found another holder", raw)
            return
        _journal_lock(slug, caller, "released", raw)


def keep_tick(
    store: RedisStore, slug: str, held: lease.Lease, token: str, ttl_ms: int, caller: str = "unnamed"
) -> None:
    from redis.exceptions import WatchError

    lease.renew(store, slug, held)
    key = store.key(slug, "tick-lock")
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw = _lock_raw(pipe, key)
                if raw != token:
                    raise SwarmError(
                        f"the tick lock is stale: {caller} expected token {_token_prefix(token)}, {_lock_holder(raw)}"
                    )
                pipe.multi()
                pipe.pexpire(key, ttl_ms)
                pipe.execute()
                return
            except WatchError:
                continue


def _token_prefix(value: str) -> str:
    decoded: dict[str, Any] = json.loads(value) if value.startswith("{") else {}
    return str(decoded.get("token", value))[:8]


def _lock_holder(raw: str | None) -> str:
    if raw is None:
        return "the key is absent"
    held: dict[str, Any] = json.loads(raw) if raw.startswith("{") else {}
    fields = " ".join(f"{name} {held.get(name, 'unknown')}" for name in ("epoch", "host", "pid", "start"))
    return f"held by {fields} token {_token_prefix(raw)}"


def _journal_lock(slug: str, caller: str, outcome: str, raw: str | None) -> None:
    line = {"event": "swarm_tick_lock", "slug": slug, "caller": caller, "outcome": outcome, "holder": _lock_holder(raw)}
    sys.stderr.write(json.dumps(line) + "\n")
    sys.stderr.flush()


def run_once(
    store: RedisStore,
    ledger: Any = None,
    runtime: Any = None,
    messenger: Any = None,
    runtimes: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    from scripts.swarm.cli import run_tick
    from scripts.swarm.commands import hive_id

    runtimes = runtimes or {}
    host = hive_id()
    actions: dict[str, list[str]] = {}
    reader: Any = ledger or LedgerClient()
    for slug in ticked(store.slugs(), os.environ.get(HOST_SWARMS_ENV, "")):
        if not tickable(reader, slug):
            continue
        store.redis.set(store.key(slug, "controller-host"), host, px=lease.ttl_ms())
        actions[slug] = run_tick(store, slug, ledger, runtimes.get(slug, runtime), messenger, scheduled=True)
    return actions


def tickable(ledger: Any, slug: str) -> bool:
    """Answer False for a swarm whose ledger is binned or closed; a gone or cleartext ledger ticks so its pass
    reports it once, and a ledger that cannot be reached ends the pass as unreachable."""
    try:
        return not (ledger.binned(slug) or ledger.closed(slug))
    except (LedgerGone, ledger_link.Cleartext):
        return True
    except OSError as exc:
        raise LedgerUnreachable(f"ledger {slug}: {exc}") from exc


def tick_once(store: RedisStore, service: "ControlService | None") -> dict:
    if service is None or service.runtime is None:
        return run_once(store)
    return run_once(store, runtimes={service.controller.slug: service.runtime})


def bus_user(credentials: str | None) -> str:
    return nkey.claims(nkey.parse_creds(credentials)[0])["name"] if credentials else "anonymous"


def open_controller_bus(environ: Mapping[str, str], opener=open_bus):
    if environ.get(BACKEND_ENV) != "nats":
        return None
    try:
        config = BusConfig.from_environ(environ)
        user = bus_user(config.credentials)
        bus = opener(replace(config, provision=False))
    except Exception as exc:
        print(f"controller: bus nats refused: {exc}", file=sys.stderr, flush=True)
        return None
    print(f"controller: bus nats at {config.url} as {user}", flush=True)
    return bus


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
RUNNING = threading.Event()
DRAINING = threading.Event()
STOPPING = threading.Event()
LOST_EXIT = 75


def _halt() -> NoReturn:
    """Stop the tick, release every held tick lock and exit as a terminated process."""
    tick_stop.STOP.set()
    tick_stop.release_all()
    sys.exit(143)


def _stopped(signum: int, frame: object) -> None:
    """Let a running pass finish on the first stop signal; exit at once between passes or on a second signal."""
    if RUNNING.is_set() and not DRAINING.is_set():
        DRAINING.set()
        os.write(2, f"controller: stop signal {signum}, finishing the running pass before exit\n".encode())
        return
    _halt()


def _cadence_wait(started: float) -> None:
    """Wait out the rest of one tick from the pass start, so passes start one tick apart."""
    left = lease.tick_ms() / 1000 - (monotonic() - started)
    if left > 0:
        scheduled_wait(left)


def _pass(store: RedisStore, service: "ControlService | None", proof_swarm: ModuleType) -> None:
    """Run one controller pass: tick every hosted swarm, then the control service and the proof swarms."""
    for slug, actions in tick_once(store, service).items():
        for action in actions:
            print(f"{slug}: {action}", flush=True)
    if service is not None:
        service.tick()
    proof_swarm.run(os.environ, store, service.controller.require if service is not None else None)


def _finishing(
    store: RedisStore,
    service: "ControlService | None",
    proof_swarm: ModuleType,
    lock: replica.ReplicaLock | None = None,
) -> None:
    """Run one pass that a stop signal lets finish, then exit once the pass ends; a replica first renews its lock
    and ends the process when another replica holds it."""
    if lock is not None and not lock.take():
        _lost()
    RUNNING.set()
    try:
        _pass(store, service, proof_swarm)
    except Exception as exc:
        if not DRAINING.is_set():
            raise
        os.write(2, f"controller: the last pass before exit ended: {exc}\n".encode())
    finally:
        RUNNING.clear()
    if DRAINING.is_set():
        _halt()


def _reached(outage: redis_grace.Outage) -> RedisStore:
    """Connect at startup, retrying one tick apart while Redis refuses inside the grace window."""
    while True:
        try:
            store = connect()
        except Unreachable as exc:
            if not outage.tolerated():
                raise
            print(f"controller: {exc}; retrying after one tick inside the Redis grace window", file=sys.stderr)
            scheduled_wait(lease.tick_ms() / 1000)
            continue
        outage.ended()
        return store


def _journal_pass(number: int, outcome: str) -> None:
    """Write the one structured line that marks the end of a controller pass and names its outcome."""
    sys.stdout.write(json.dumps({"event": "controller_pass_end", "pass": number, "outcome": outcome}) + "\n")
    sys.stdout.flush()


def _lead(
    store: RedisStore,
    environ: Mapping[str, str],
    lock: replica.ReplicaLock,
    wait: Callable[[float], None] = scheduled_wait,
) -> None:
    """Stand by behind the replica leading this hive's passes, then take the lead and keep it renewed."""
    from scripts.swarm_v2 import control_service

    servers = control_service.stand_by(environ, store)
    try:
        if not lock.take():
            print(f"controller: replica {lock.owner} stands by while {lock.holder()} leads", flush=True)
            lock.lead(wait)
    finally:
        control_service.close(servers)
    print(f"controller: replica {lock.owner} leads the passes", flush=True)
    lock.beat(_lost, STOPPING)


def _lost(end: Callable[[int], None] = os._exit) -> None:
    """End the process at once when another replica took the lead, so two replicas never run passes together."""
    os.write(2, b"controller: another replica took the lead; exiting\n")
    end(LOST_EXIT)


def _keep_credential(environ: Mapping[str, str], store: RedisStore) -> threading.Thread | None:
    """Keep a replica's own controller credential alive for as long as the controller runs."""
    from scripts.hive import auth
    from scripts.swarm_v2.control_service import CREDENTIAL_ENV

    credential = environ.get(CREDENTIAL_ENV)
    if not credential or "." not in credential:
        return None
    keeping = partial(auth.keep_service, store.redis, credential)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
    return replica.keep(keeping, STOPPING, replica.KEEP_S)  # pyright: ignore[reportUnknownArgumentType]


def _passes(
    store: RedisStore,
    service: "ControlService | None",
    proof_swarm: ModuleType,
    once: bool,
    outage: redis_grace.Outage,
    lock: replica.ReplicaLock | None = None,
) -> int:
    """Run passes one tick apart, riding out Redis errors until they outlast the grace window."""
    if not once:
        intent.keep_judging()
    number = 0
    while True:
        started = monotonic()
        number += 1
        try:
            _finishing(store, service, proof_swarm, lock)
        except LedgerUnreachable as exc:
            print(f"controller: pass ended, next pass after one tick: {exc}", file=sys.stderr, flush=True)
            _journal_pass(number, "ledger_unreachable")
            if once:
                return 1
            scheduled_wait(lease.tick_ms() / 1000)
            continue
        except store_errors() as exc:
            if not outage.tolerated():
                raise Unreachable(f"Redis is unreachable past the {outage.grace_s:.0f}s grace window ({exc})") from exc
            print(
                f"controller: pass ended inside the Redis grace window, next pass after one tick: {exc}",
                file=sys.stderr,
            )
            _journal_pass(number, "redis_grace")
            if once:
                return 1
            scheduled_wait(lease.tick_ms() / 1000)
            continue
        outage.ended()
        redis_grace.cleared(redis_grace.marker(os.environ))
        _journal_pass(number, "ok")
        if once:
            return 0
        _cadence_wait(started)


def _probe(action: str) -> int:
    """Run one container probe; liveness passes while a Redis outage is inside the grace window."""
    from scripts.swarm import controller_probes

    path = redis_grace.marker(os.environ)
    try:
        status = getattr(controller_probes, action)(os.environ, connect())
    except (SwarmError, *store_errors()) as exc:
        if action == "live" and isinstance(exc, (Unreachable, *store_errors())):
            grace = redis_grace.grace_seconds(os.environ)
            down = redis_grace.down_for(path, time())
            if down < grace:
                print(f"controller: {exc}; live inside the {grace:.0f}s Redis grace window, down {down:.0f}s")
                return 0
        print(f"controller: {exc}")
        return 1
    if action == "live":
        redis_grace.cleared(path)
    return status


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks controller")
    parser.add_argument("action", choices=("run", "live", "ready"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.action != "run":
        return _probe(args.action)
    from scripts import operator_env
    from scripts.swarm import commands
    from scripts.swarm_v2 import control_service, proof_swarm

    operator_env.fill(os.environ)
    operator_env.classifier(os.environ)
    outage = redis_grace.Outage.from_environ()
    store = _reached(outage)
    service, lock = None, None
    bus = open_controller_bus(os.environ)
    DRAINING.clear()
    STOPPING.clear()
    previous = {number: signal.signal(number, _stopped) for number in STOP_SIGNALS}
    try:
        _keep_credential(os.environ, store)
        lock = replica.configured(os.environ, store, commands.hive_id)
        if lock is not None:
            _lead(store, os.environ, lock)
        proof_swarm.run(os.environ, store)
        service = control_service.host(os.environ, store, commands.hive_id())
        return _passes(store, service, proof_swarm, args.once, outage, lock)
    except SwarmError as exc:
        print(f"controller: {exc}", file=sys.stderr)
        return 1
    finally:
        STOPPING.set()
        if service is not None:
            service.stop()
        if lock is not None:
            lock.release()
        if bus is not None:
            bus.close()
        for number, handler in previous.items():
            signal.signal(number, handler)
