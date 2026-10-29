import json
import os
import re
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

from scripts.swarm_v2.herdr.capabilities import Incompatible, Qualifier, runner
from scripts.swarm_v2.herdr.catalog import Catalog
from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.reconcile import Record, Store

PANE = re.compile(r"w[0-9A-Z]+:p[0-9A-Z]+")
CALLER = ("HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "HERDR_TAB_ID", "HERDR_SOCKET_PATH")
READS = frozenset({"pane_read", "pane_wait_output", "pane_process_info", "agent_get"})
MUTATIONS = frozenset({"pane_run", "pane_send_text", "pane_send_keys", "pane_close", "agent_prompt", "agent_rename"})
LISTING = "agent_list"
DELIVERIES = "deliveries.json"
SET_ASIDE = ".set-aside.jsonl"
VERSION = 1
KEPT = 1024
PENDING_LIMIT = 64
EXPIRY_S = 600
REASON_CHARS = 200
BARE = "a remote pane operation needs a saved machine profile, a remote session and a server incarnation"

Run = Callable[[list[str]], subprocess.CompletedProcess]


@dataclass(frozen=True)
class Target:
    """The incarnation is the execution id and generation of the one herdr server that owns the pane."""

    profile_id: str
    session: str
    incarnation: str
    pane_id: str
    conversation_id: str = ""


def remote_environ(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if name not in CALLER}


def client_runner(environ: Mapping[str, str]) -> Run:
    return runner(remote_environ(environ))


def _incarnation(record: Record) -> str:
    return f"{record.execution_id}/{record.generation}"


def _qualified(target: object) -> bool:
    return isinstance(target, Target) and all((target.profile_id, target.session, target.incarnation))


def pane_conversation(reply: str) -> tuple[str, str] | None:
    try:
        document = json.loads(reply)
    except ValueError:
        return None
    result = document.get("result") if isinstance(document, dict) else None
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return None
    session = agent.get("agent_session")
    found = session.get("value") if isinstance(session, dict) and session.get("kind") == "id" else ""
    return str(agent.get("pane_id")), found or ""


def _agent_panes(reply: str) -> list[str] | None:
    try:
        document = json.loads(reply)
    except ValueError:
        return None
    result = document.get("result") if isinstance(document, dict) else None
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return None
    return [agent["pane_id"] for agent in agents if isinstance(agent, dict) and agent.get("pane_id")]


def _expire(entries: dict[str, dict], now: int) -> None:
    for key, entry in entries.items():
        if entry.get("state") == "pending" and now - entry.get("since", 0) >= EXPIRY_S:
            entries[key] = {**entry, "state": "expired"}


def _make_room(entries: dict[str, dict]) -> None:
    pending = sorted((entry.get("since", 0), key) for key, entry in entries.items() if entry.get("state") == "pending")
    for _, key in pending[: max(len(pending) - PENDING_LIMIT + 1, 0)]:
        entries[key] = {**entries[key], "state": "expired"}


def _refusal(done: subprocess.CompletedProcess) -> str:
    detail = " ".join((done.stderr or "").split())[:REASON_CHARS]
    return f"herdr exited {done.returncode}: {detail}" if detail else f"herdr exited {done.returncode}"


def listing(entries: Mapping[str, dict], now: int) -> dict[str, list[dict]]:
    current = dict(entries)
    _expire(current, now)
    rows = [{"operation_id": key, **entry} for key, entry in current.items()]
    return {
        "pending": [row for row in rows if row.get("state") == "pending"],
        "settled": [row for row in rows if row.get("state") != "pending"],
    }


class Deliveries:
    """Durable operation ids: a pending entry is a send whose outcome is unknown and is never sent again."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.locked = Store(path).locked

    def load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text())
            entries = document["deliveries"]
            if document["version"] != VERSION:
                raise ValueError(f"not a version {VERSION} delivery journal")
            if not isinstance(entries, dict):
                raise ValueError("deliveries is not an object")
        except (OSError, ValueError, TypeError, KeyError):
            raise EndpointRefused("journal_unreadable", "the pane delivery journal is unreadable") from None
        return entries

    def recover(self, now: int) -> dict[str, dict]:
        try:
            return self.load()
        except EndpointRefused as refused:
            cause = refused.__context__
            if isinstance(cause, OSError):
                raise
            aside = self.path.with_name(f"{self.path.name}.unreadable-{now}")
            record = {"at": now, "file": aside.name, "reason": f"{type(cause).__name__}: {cause}"}
            log = self.path.with_name(f"{self.path.name}{SET_ASIDE}")
            try:
                os.link(self.path, aside)
            except OSError:
                raise refused from None
            try:
                with open(log, "a", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
                    handle.write(json.dumps(record) + "\n")
                self.path.unlink()
            except OSError:
                with suppress(OSError):
                    aside.unlink()
                raise refused from None
        return {}

    def save(self, entries: dict[str, dict]) -> None:
        settled = [key for key, entry in entries.items() if entry.get("state") != "pending"]
        dropped = set(settled[: max(len(settled) - KEPT, 0)])
        kept = {key: entry for key, entry in entries.items() if key not in dropped}
        staged = self.path.with_name(f".{self.path.name}.tmp")
        staged.unlink(missing_ok=True)
        with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(json.dumps({"version": VERSION, "deliveries": kept}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(self.path)
        folder = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


class Router:
    """Every remote pane operation names one saved machine profile owned by the controller and one confirmed server
    incarnation; it never falls back to the local server. A target without an expected conversation is sent on its pane
    id and incarnation alone."""

    def __init__(
        self, store: Store, catalog: Catalog, qualifier: Qualifier, run: Run, journal: Path, *, mutations: bool = True
    ) -> None:
        self.store, self.catalog, self.qualifier, self.run = store, catalog, qualifier, run
        self.deliveries, self.mutations = Deliveries(journal), mutations
        self.mismatches: Counter = Counter()
        self.clock: Callable[[], float] = time.time

    def _refuse(self, reason: str, message: str, profile_id: str = "bare") -> NoReturn:
        self.mismatches[(profile_id, reason)] += 1
        raise EndpointRefused(reason, message)

    def _pane(self, profile_id: str, pane_id: str) -> None:
        if not isinstance(pane_id, str) or not PANE.fullmatch(pane_id):
            self._refuse("pane_id", "a herdr pane id reads w<id>:p<id>, each id digits and capital letters", profile_id)

    def _listed(self, record: Record) -> None:
        saved = next((machine for machine in self.catalog.machines() if machine.profile_id == record.profile_id), None)
        wanted = (record.label, record.target, record.session, True)
        if saved is None or (saved.label, saved.target, saved.session, saved.enabled) != wanted:
            self._refuse("unregistered", f"{record.label} is not saved on this herdr client", record.profile_id)

    def _owned(self, profile_id: str) -> Record:
        record = next((entry for entry in self.store.load().values() if entry.profile_id == profile_id), None)
        if record is None:
            self._refuse("unregistered", f"machine {profile_id} is not registered by this controller", profile_id)
        self._listed(record)
        return record

    def resolve(self, label: str, pane_id: str, conversation_id: str = "") -> Target:
        found = [entry for entry in self.store.load().values() if entry.label == label and entry.profile_id]
        if len(found) != 1:
            self._refuse("unregistered", f"{label} is not one machine this controller registered")
        record = found[0]
        self._pane(record.profile_id, pane_id)
        self._listed(record)
        return Target(record.profile_id, record.session, _incarnation(record), pane_id, conversation_id)

    def refresh(self, target: Target) -> Target:
        if not _qualified(target):
            self._refuse("bare_pane", BARE)
        execution_id = target.incarnation.rpartition("/")[0]
        record = self.store.load().get(execution_id)
        if record is None or record.profile_id is None:
            self._refuse("unregistered", f"{execution_id} has no registered terminal", target.profile_id)
        if _incarnation(record) != target.incarnation:
            self._refuse("stale_incarnation", f"{target.incarnation} was replaced", target.profile_id)
        self._listed(record)
        return replace(target, profile_id=record.profile_id, session=record.session)

    def command(self, target: Target, operation: str, *args: str) -> list[str]:
        if not _qualified(target):
            self._refuse("bare_pane", BARE)
        if operation not in READS | MUTATIONS:
            self._refuse("operation", f"{operation} does not address one pane", target.profile_id)
        self._pane(target.profile_id, target.pane_id)
        return [*self._server(target, operation), target.pane_id, *args]

    def _server(self, target: Target, operation: str) -> list[str]:
        record = self._owned(target.profile_id)
        if record.session != target.session:
            self._refuse("session_mismatch", f"{record.label} is saved for another remote session", target.profile_id)
        if _incarnation(record) != target.incarnation:
            self._refuse("stale_incarnation", f"{target.incarnation} was replaced", target.profile_id)
        try:
            base = self.qualifier.require(target.profile_id, target.incarnation, operation)
        except Incompatible as exc:
            self._refuse("incompatible", str(exc), target.profile_id)
        return base

    def agent_panes(self, target: Target) -> list[str]:
        """The panes the target's server lists as agents; the target's own pane id is not read."""
        if not _qualified(target):
            self._refuse("bare_pane", BARE)
        argv = self._server(target, LISTING)
        try:
            done = self.run(argv)
        except (OSError, subprocess.TimeoutExpired):
            done = None
        if done is None or done.returncode != 0:
            raise EndpointRefused("unreachable", f"{target.incarnation} did not list its agents")
        panes = _agent_panes(done.stdout)
        if panes is None:
            raise EndpointRefused("agent_list", f"{target.incarnation} answered an unreadable agent list")
        return panes

    def _confirm(self, target: Target) -> None:
        if not target.conversation_id:
            return
        argv = self.command(target, "agent_get")
        try:
            done = self.run(argv)
        except (OSError, subprocess.TimeoutExpired):
            done = None
        found = pane_conversation(done.stdout) if done is not None and done.returncode == 0 else None
        if found is None or not found[1]:
            self._refuse("conversation_unconfirmed", f"{target.pane_id} reported no conversation", target.profile_id)
        if found != (target.pane_id, target.conversation_id):
            self._refuse("conversation_mismatch", f"{target.pane_id} holds another conversation", target.profile_id)

    def send(self, target: Target, operation: str, operation_id: str, *args: str) -> subprocess.CompletedProcess | None:
        if not self.mutations:
            raise EndpointRefused("read_only", "pane mutations are switched off; only read only inspection runs")
        if not _qualified(target):
            self._refuse("bare_pane", BARE)
        if operation not in MUTATIONS:
            self._refuse("operation", f"{operation} is not a pane mutation", target.profile_id)
        if not isinstance(operation_id, str) or not operation_id:
            raise EndpointRefused("operation_id", "a pane mutation needs an operation id")
        key = f"{target.incarnation}/{target.pane_id}"
        with self.deliveries.locked():
            now = int(self.clock())
            entries = self.deliveries.recover(now)
            held = entries.get(operation_id)
            if held is not None:
                return self._replayed(held, target, key, operation_id)
            _expire(entries, now)
            _make_room(entries)
            argv = self.command(target, operation, *args)
            self._confirm(target)
            entries[operation_id] = {"target": key, "state": "pending", "since": now}
            self.deliveries.save(entries)
            try:
                done = self.run(argv)
            except (OSError, subprocess.TimeoutExpired):
                raise EndpointRefused("ambiguous_delivery", f"{operation_id} may have reached {key}") from None
            if done.returncode != 0:
                entries[operation_id] = {"target": key, "state": "refused", "reason": _refusal(done)}
                self.deliveries.save(entries)
                raise EndpointRefused("send_refused", f"herdr exited {done.returncode} for {operation_id} on {key}")
            entries[operation_id] = {"target": key, "state": "sent"}
            self.deliveries.save(entries)
        return done

    def _replayed(self, held: dict, target: Target, key: str, operation_id: str) -> None:
        if held.get("target") != key:
            self._refuse("operation_reused", f"{operation_id} was sent to another pane", target.profile_id)
        if held.get("state") == "refused":
            raise EndpointRefused("send_refused", f"{operation_id} was refused on {key}: {held.get('reason')}")
        if held.get("state") != "sent":
            raise EndpointRefused("ambiguous_delivery", f"{operation_id} may have reached {key}")

    def settle(self, operation_id: str, delivered: bool) -> None:
        """The operator's audit of a send whose outcome was unknown: delivered keeps its id as sent, otherwise the id
        is released so a new request may use it."""
        with self.deliveries.locked():
            entries = self.deliveries.load()
            held = entries.get(operation_id)
            if held is None or held.get("state") not in ("pending", "expired"):
                raise EndpointRefused("not_pending", f"{operation_id} has no unknown outcome to settle")
            if delivered:
                entries[operation_id] = {**held, "state": "sent"}
            else:
                del entries[operation_id]
            self.deliveries.save(entries)

    def herdr_target_mismatch_total(self) -> int:
        return sum(self.mismatches.values())
