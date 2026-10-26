"""A Pod worker holds its task lease only while it renews it: the controller admits with a short lease and refuses
every scoped write once it lapses. The launch grant that authenticates each renewal expires too and is renewed here.
The start renews from registration through its slow preparation, then hands the lease to the supervisor on disk."""

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path

from scripts.swarm_v2 import broadcast_bridge
from scripts.swarm_v2.broadcast_bridge import AUTHORIZATION, JSON_HEADERS

ADMISSION = "admission.json"
SCHEMA_VERSION = "2.1"
HEARTBEAT = "/v2/executions/{}/heartbeat"
CREDENTIAL = "/v2/executions/{}/credential"
INTERVAL_SECONDS = 15.0
RETRY_SECONDS = 2.0
CREDENTIAL_SECONDS = 60.0
PAUSE_SECONDS = 1.0
TIMEOUT_SECONDS = 5
UNREACHABLE = 503
REFUSED = (400, 401, 403, 404, 409)
AUTHORITY = ("execution_id", "task_id", "task_generation", "controller_epoch", "owner_identity")
DEADLINE = "lease_deadline_ms"
EXPECTED = {"heartbeat": (DEADLINE, int), "credential": ("credential", str)}

Send = Callable[[str, str, str, dict], tuple[int, object]]


def send(method: str, url: str, grant: str, body: dict, timeout: float = TIMEOUT_SECONDS) -> tuple[int, object]:
    headers = {**JSON_HEADERS, AUTHORIZATION: f"Bearer {grant}"}
    request = urllib.request.Request(url, json.dumps(body).encode(), headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            status, text = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (OSError, HTTPException):
        return UNREACHABLE, {}
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {}


def _timestamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


@dataclass(frozen=True)
class Lease:
    attempt: Path
    control_url: str
    admission: dict
    grant: str


class Renewer:
    def __init__(
        self,
        lease: Lease,
        send: Send = send,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.attempt, self.send, self.clock, self.wall = lease.attempt, send, clock, wall
        self.url = lease.control_url.rstrip("/")
        self.grant = lease.grant
        self.authority = {key: lease.admission[key] for key in AUTHORITY}
        now = clock()
        self.deadline_ms = lease.admission[DEADLINE]
        self.deadline = self._until(self.deadline_ms)
        self.due = now
        self.credential_due = now + CREDENTIAL_SECONDS
        self.sequence = 0
        self.lost: str | None = None

    def record(self) -> dict:
        return {**self.authority, DEADLINE: self.deadline_ms}

    def tick(self) -> None:
        now = self.clock()
        if self.lost is not None:
            return
        if now >= self.deadline:
            self.lose("lease_expired")
            return
        if now < self.due:
            return
        if now >= self.credential_due and not self._credential(now):
            return
        answer = self._call("heartbeat", "PUT", HEARTBEAT, self._body())
        if answer is None:
            return
        if (answer.get("execution_id"), answer.get("task_generation")) != (
            self.authority["execution_id"],
            self.authority["task_generation"],
        ):
            self.lose("generation_changed")
            return
        self.authority["controller_epoch"] = answer["controller_epoch"]
        self.deadline_ms = answer[DEADLINE]
        self.deadline = self._until(self.deadline_ms)
        self.due = now + INTERVAL_SECONDS
        self._log("renewed", renewal_sequence=self.sequence, lease_deadline_ms=self.deadline_ms)

    def _credential(self, now: float) -> bool:
        answer = self._call("credential", "POST", CREDENTIAL, {})
        if answer is None:
            return False
        self.grant = answer["credential"]
        path = broadcast_bridge.grant_path(self.attempt)
        if path.parent.is_dir():
            broadcast_bridge.store_grant(path, self.grant)
        self.credential_due = now + CREDENTIAL_SECONDS
        self._log("credential_renewed", expires_at=answer.get("expires_at"))
        return True

    def _call(self, call: str, method: str, path: str, body: dict) -> dict | None:
        url = self.url + path.format(self.authority["execution_id"])
        status, answer = self.send(method, url, self.grant, body)
        if status in REFUSED:
            self.lose(f"{call}_refused_{status}")
            return None
        key, kind = EXPECTED[call]
        if status != 200 or not isinstance(answer, dict) or type(answer.get(key)) is not kind or not answer[key]:
            self.due = self.clock() + RETRY_SECONDS
            self._log("renewal_failed", call=call, status=status, retry_seconds=RETRY_SECONDS)
            return None
        return answer

    def _body(self) -> dict:
        self.sequence = max(self.sequence + 1, int(self.wall() * 1000))
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_id": f"heartbeat-{uuid.uuid4().hex}",
            "authority": dict(self.authority),
            "renewal_sequence": self.sequence,
            "state": "working",
            "observed_at": _timestamp(self.wall()),
        }

    def _until(self, deadline_ms: int) -> float:
        return self.clock() + deadline_ms / 1000 - self.wall()

    def lose(self, reason: str) -> None:
        self.lost = reason
        self._log("lost", reason=reason)

    def _log(self, event: str, **fields: object) -> None:
        identity = {
            "execution_id": self.authority["execution_id"],
            "task_generation": self.authority["task_generation"],
        }
        print(json.dumps({"lease": event, **fields, **identity}), flush=True)


class Unleased:
    lost: str | None = None

    def tick(self) -> None:
        pass


@contextmanager
def kept(renewer: Renewer, pause: float = PAUSE_SECONDS) -> Iterator[Renewer]:
    stop = threading.Event()

    def renew() -> None:
        while renewer.lost is None:
            try:
                renewer.tick()
            except Exception as error:
                renewer.lose(f"renewal_crashed_{type(error).__name__}: {error}")
                return
            if stop.wait(pause):
                return

    thread = threading.Thread(target=renew, name="lease-renewal", daemon=True)
    thread.start()
    try:
        yield renewer
    finally:
        stop.set()
        thread.join()


def load(
    attempt: Path, send: Send = send, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time
) -> Renewer | Unleased:
    if not (attempt / ADMISSION).exists():
        return Unleased()
    admission = json.loads((attempt / ADMISSION).read_text())
    control_url = json.loads((attempt / "launch.json").read_text())["control_url"]
    grant = broadcast_bridge.grant_path(attempt).read_text().strip()
    return Renewer(Lease(attempt, control_url, admission, grant), send, clock, wall)
