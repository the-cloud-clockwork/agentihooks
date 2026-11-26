"""The CI lab worker pod loop: lease units from the scheduler's lease API into the pod's free slots, run each on the
executor its kind names side by side, heartbeat each lease while its unit runs, upload the unit's report and log
through presigned URLs and complete the lease.

The worker speaks only HTTP to the scheduler and holds no credentials: its environment refuses every GitHub App and
Redis variable. A unit whose executor raises stops heartbeating and is never completed, so the scheduler reaps its
lease and requeues it once.
"""

import contextlib
import functools
import http.client
import json
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from scripts import ci_cpu_quota
from scripts.ci_lab import contract, worktrees
from scripts.ci_lab.outcomes import COMPLETED
from scripts.ci_lab.units import Unit
from scripts.scheduler.process_interval import Interval

__all__ = [
    "CAPABILITIES",
    "GRACE_SECONDS",
    "HEARTBEAT_SECONDS",
    "UNIT_TIMEOUT_SECONDS",
    "Client",
    "Executor",
    "Finished",
    "Lease",
    "Refused",
    "Worker",
    "build",
    "environment",
    "slots",
]

CAPABILITIES = frozenset({"net", "browser"})
LEASE_WAIT_MS = 5_000
BACKOFF_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 10.0
HEARTBEAT_SECONDS = 10.0
UNIT_TIMEOUT_SECONDS = 1_800.0
TIMEOUT_SECONDS = 60.0
DELIVERY_CALLS = 5
GRACE_SECONDS = UNIT_TIMEOUT_SECONDS + DELIVERY_CALLS * TIMEOUT_SECONDS
REFUSED = 403
RESTART = 426
SERVER_ERROR = 500
REDIS = "REDIS"


@dataclass(frozen=True)
class Lease:
    """One leased unit: its job, the lease token, the head it grades, the unit, its slots, the lease expiry and the base
    and grader revision its grade reads."""

    job: str
    token: str
    head: str
    unit: Unit
    slots: int
    expires_ms: int
    base: str = ""
    grader: str = ""


@dataclass(frozen=True)
class Finished:
    """An executor's answer for one unit: its outcome and the report and log files the worker uploads."""

    outcome: str
    report: Path
    log: Path

    def __post_init__(self) -> None:
        """Refuse an outcome the leases refuse, so an executor answering one raises and its lease is reaped."""
        if self.outcome not in COMPLETED:
            raise ValueError(f"unknown lab unit outcome {self.outcome!r}")


Executor = Callable[[Lease, Path], Finished]


class Refused(RuntimeError):
    """The worker environment holds a GitHub App or Redis variable."""


class Lost(RuntimeError):
    """The scheduler refused a heartbeat's lease token, so the lease is no longer this worker's."""


def _lease(grant: Mapping[str, Any]) -> Lease:
    """Read one grant of the lease API's POST /lease reply."""
    unit = Unit(
        kind=grant["kind"],
        key=grant["key"],
        members=tuple(grant["members"]),
        manifest=grant.get("manifest", ""),
        manifest_digest=grant.get("manifest_digest", ""),
    )
    return Lease(
        grant["job"],
        grant["token"],
        grant["head"],
        unit,
        grant["slots"],
        grant["expires_ms"],
        grant["base"],
        grant["grader"],
    )


@dataclass(frozen=True)
class Client:
    """The scheduler's lease API over HTTP; a call the scheduler refuses for its lease token answers None or False."""

    url: str
    timeout: float = TIMEOUT_SECONDS

    def _send(self, path: str, body: Mapping[str, object] | None) -> dict[str, Any]:
        """POST a JSON body to the path, or GET it without one, and return the JSON reply."""
        data = None if body is None else json.dumps(body).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url + path, data=data), timeout=self.timeout) as reply:
            return json.loads(reply.read())

    def _held(self, path: str, body: Mapping[str, object] | None) -> dict[str, Any] | None:
        """Send a call that needs the lease token; None when the scheduler refuses the token."""
        try:
            return self._send(path, body)
        except urllib.error.HTTPError as error:
            if error.code != REFUSED:
                raise
            return None

    def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[Lease]:
        """Long poll for the pending units that fit the free slots and the capabilities."""
        body = {"worker": worker, "free_slots": free_slots, "capabilities": sorted(capabilities), "wait_ms": wait_ms}
        return [_lease(grant) for grant in self._send("/lease", body)["leases"]]

    def heartbeat(self, job: str, token: str) -> int | None:
        """Extend the lease and return its new expiry, or None when the token no longer holds it."""
        reply = self._held("/heartbeat", {"job": job, "token": token})
        return None if reply is None else reply["expires_ms"]

    def upload_url(self, job: str, token: str, name: str) -> str | None:
        """Return the presigned URL of the job's report or log, or None when the token no longer holds the lease."""
        reply = self._held("/upload-url?" + urlencode({"job": job, "token": token, "name": name}), None)
        return None if reply is None else reply["url"]

    def complete(self, job: str, token: str, outcome: str) -> bool:
        """Record the job's outcome; False when the token no longer holds the lease."""
        return self._held("/complete", {"job": job, "token": token, "outcome": outcome}) is not None

    def put(self, url: str, path: Path) -> None:
        """Upload a file's bytes to a presigned PUT URL."""
        request = urllib.request.Request(url, data=path.read_bytes(), method="PUT")
        urllib.request.urlopen(request, timeout=self.timeout).close()


@dataclass
class Worker:
    """One pod's loop: lease units into the free slots, run them side by side on their executors, heartbeat each
    lease while its unit runs, then upload the unit's report and log and complete its lease."""

    client: Client
    executors: Mapping[str, Executor]
    name: str
    slots: int
    runs: Path
    capabilities: frozenset[str] = CAPABILITIES
    wait_ms: int = LEASE_WAIT_MS
    heartbeat_s: float = HEARTBEAT_SECONDS
    grace_s: float = GRACE_SECONDS
    stopping: threading.Event = field(default_factory=threading.Event)
    abandoned: threading.Event = field(default_factory=threading.Event)
    running: dict[Future[None], Lease] = field(default_factory=dict[Future[None], Lease])
    pool: ThreadPoolExecutor = field(init=False)
    backoff_s: float = field(default=BACKOFF_SECONDS, init=False)

    def __post_init__(self) -> None:
        """Give every slot a thread to run a unit on."""
        self.pool = ThreadPoolExecutor(max_workers=self.slots)

    def free(self) -> int:
        """Return the slots no running unit holds, after reporting each unit that crashed since the last look with
        the exception's traceback."""
        for future in [future for future in self.running if future.done()]:
            lease = self.running.pop(future)
            if (error := future.exception()) is not None:
                print(f"ci-lab worker: unit {lease.job} crashed: {error!r}", file=sys.stderr)
                traceback.print_exception(error)
        return self.slots - sum(lease.slots for lease in self.running.values())

    def run_once(self) -> list[Lease]:
        """Lease units into the free slots and start each on its executor, or wait for a running unit to end while
        every slot is busy; return the leases started. A lease call the scheduler does not answer, or answers with a
        server error, starts nothing and waits out its backoff; a restart answer stops the worker without raising or
        backing off, since the lab wants another image; any other error answer raises."""
        free = self.free()
        if free <= 0:
            wait(list(self.running), return_when=FIRST_COMPLETED)
            return []
        try:
            leases = self.client.lease(self.name, free, self.capabilities, self.wait_ms)
        except (OSError, http.client.HTTPException) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code == RESTART:
                print("ci-lab worker: the lab wants another image, restarting", file=sys.stderr)
                self.stop()
                return []
            if isinstance(error, urllib.error.HTTPError) and error.code < SERVER_ERROR:
                raise
            self._back_off(error)
            return []
        self.backoff_s = BACKOFF_SECONDS
        for lease in leases:
            self.running[self.pool.submit(self._unit, lease)] = lease
        return leases

    def _back_off(self, error: OSError | http.client.HTTPException) -> None:
        """Report a lease call the scheduler did not answer and wait out its backoff, which doubles with each such call
        in a row up to BACKOFF_CAP_SECONDS; stop ends the wait at once."""
        delay = self.backoff_s
        self.backoff_s = min(delay * 2, BACKOFF_CAP_SECONDS)
        print(f"ci-lab worker: lease call failed, retrying in {delay:g}s: {error!r}", file=sys.stderr)
        self.stopping.wait(delay)

    def stop(self) -> None:
        """Lease no more units; run then lets the running ones finish within the termination grace."""
        self.stopping.set()

    def run(self) -> list[Lease]:
        """Lease and run units until stop, then drain; return the leases still running when the grace ended. A unit
        whose executor ends after that stops heartbeating and is never delivered, so the scheduler reaps it."""
        while not self.stopping.is_set():
            self.run_once()
        left = self.drain()
        self.abandoned.set()
        return left

    def drain(self) -> list[Lease]:
        """Wait up to the termination grace for the running units to end; return the leases still running."""
        wait(list(self.running), timeout=self.grace_s)
        self.free()
        return list(self.running.values())

    def _unit(self, lease: Lease) -> None:
        """Run one unit in its own fresh folder under runs while its lease heartbeats, then deliver it unless its
        lease was lost or run abandoned it; an executor that raises leaves it to reap, and a heartbeat that hangs
        only ends the heartbeats. The folder is cut before the first heartbeat, so a unit whose folder cannot be cut
        sends none and reaps. The folder is removed once the unit ends, delivered or not, after its report and log
        are read, read only folders inside it included; a folder that resists removal is left for the next
        restart's wipe."""
        root = Path(tempfile.mkdtemp(dir=self.runs))
        lost = threading.Event()
        beat = Interval(functools.partial(self._beat, lease, lost), every=self.heartbeat_s)
        beat.start()
        try:
            try:
                finished = self.executors[lease.unit.kind](lease, root)
            finally:
                with contextlib.suppress(RuntimeError):
                    beat.close()
            if not (lost.is_set() or self.abandoned.is_set()):
                self._deliver(lease, finished)
        finally:
            with contextlib.suppress(OSError):
                worktrees.empty(root)
                root.rmdir()

    def _beat(self, lease: Lease, lost: threading.Event) -> None:
        """Heartbeat the lease, skipping a beat that cannot reach the scheduler or whose reply is cut short; raise Lost
        to end its heartbeats once run abandoned the unit, or mark the lease lost first when the scheduler refuses the
        token."""
        if self.abandoned.is_set():
            raise Lost
        with contextlib.suppress(OSError, http.client.HTTPException):
            if self.client.heartbeat(lease.job, lease.token) is None:
                lost.set()
                raise Lost

    def _deliver(self, lease: Lease, finished: Finished) -> None:
        """Upload the unit's report and log through their presigned URLs and complete its lease, stopping at the
        first call the scheduler refuses for the token."""
        for name, path in (("report", finished.report), ("log", finished.log)):
            url = self.client.upload_url(lease.job, lease.token, name)
            if url is None:
                return
            self.client.put(url, path)
        self.client.complete(lease.job, lease.token, finished.outcome)


def environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the worker service contract's variables, refusing an environment that holds any GitHub App or Redis
    variable so neither the worker nor a unit process it starts holds a credential."""
    refused = sorted({*contract.github_app_variables(environ), *(name for name in environ if REDIS in name.upper())})
    if refused:
        raise Refused(f"ci-lab worker refuses {', '.join(refused)}")
    names, prefixes = contract.WORKER.names, contract.WORKER.prefixes
    return {name: value for name, value in environ.items() if name in names or name.startswith(prefixes)}


def slots(environ: Mapping[str, str], cpus: int, quota: float | None) -> int:
    """Return how many slots the pod runs: its CPU quota in whole cores, at most CI_LAB_SLOTS when that is set."""
    cores = ci_cpu_quota.worker_count(cpus, quota)
    cap = environ.get(contract.SLOTS)
    return cores if cap is None else min(cores, int(cap))


def build(
    environ: Mapping[str, str],
    executors: Mapping[str, Executor],
    name: str,
    cpus: int,
    quota: float | None,
    runs: Path,
) -> Worker:
    """Return the pod's worker over the scheduler URL its environment names, with its slots from the CPU quota."""
    allowed = environment(environ)
    return Worker(Client(allowed[contract.SCHEDULER_URL]), executors, name, slots(allowed, cpus, quota), runs)
