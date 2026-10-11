import fcntl
import json
import os
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from scripts.swarm_v2.herdr.capabilities import Incompatible, Qualifier, Unreachable
from scripts.swarm_v2.herdr.catalog import REMOTE_SESSION, Catalog, Machine
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused

STORE = "registrations.json"
VERSION = 1
ACTORS = ("grant/", "operator/")


@dataclass(frozen=True)
class Record:
    """A None profile id is an add whose outcome is unknown; profiles listed in before are never adopted for it."""

    execution_id: str
    generation: int
    label: str
    target: str
    session: str
    profile_id: str | None
    before: tuple[str, ...] = ()


def _entry(entry: dict) -> Record:
    record = Record(**entry)
    if not isinstance(record.before, list | tuple):
        raise ValueError
    before = tuple(record.before)
    texts = (record.execution_id, record.label, record.target, record.session, *before)
    if not all(isinstance(text, str) and text for text in texts):
        raise ValueError
    if type(record.generation) is not int or record.generation < 1:
        raise ValueError
    if record.profile_id is not None and not (isinstance(record.profile_id, str) and record.profile_id):
        raise ValueError
    return replace(record, before=before)


@dataclass
class Report:
    registered: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    retired: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    drift: dict[str, int] = field(default_factory=dict)

    def herdr_registration_drift(self) -> int:
        return sum(self.drift.values())


class Store:
    """The controller's ownership record: only profiles it names are ever renamed or removed."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(self.path.with_name(f".{self.path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock)

    def load(self) -> dict[str, Record]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text())
            if document["version"] != VERSION:
                raise ValueError
            records = [_entry(entry) for entry in document["registrations"]]
        except (OSError, ValueError, TypeError, KeyError):
            raise EndpointRefused("ownership_unreadable", "the registration ownership record is unreadable") from None
        return {record.execution_id: record for record in records}

    def save(self, records: dict[str, Record]) -> None:
        document = {"version": VERSION, "registrations": [asdict(records[key]) for key in sorted(records)]}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = self.path.with_name(f".{self.path.name}.tmp")
        staged.unlink(missing_ok=True)
        with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(json.dumps(document, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(self.path)
        folder = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)


def owned(record: Record | None, machines: Iterable[Machine]) -> Machine | None:
    if record is None:
        return None
    if record.profile_id is not None:
        return next((machine for machine in machines if machine.profile_id == record.profile_id), None)
    found = [
        machine
        for machine in machines
        if (machine.label, machine.target, machine.session) == (record.label, record.target, record.session)
        and machine.profile_id not in record.before
    ]
    if len(found) > 1:
        raise EndpointRefused("catalog_ambiguous", f"{record.label} was saved more than once after an interrupted add")
    return found[0] if found else None


def newest(desired: Iterable[Endpoint]) -> dict[str, Endpoint]:
    wanted: dict[str, Endpoint] = {}
    for endpoint in desired:
        held = wanted.get(endpoint.scope.execution_id)
        if held is None or endpoint.scope.generation > held.scope.generation:
            wanted[endpoint.scope.execution_id] = endpoint
    return wanted


AUDIT = "audit it and remove it with herdr machine remove before this endpoint registers"
LABEL_TAKEN = f"is the label of a machine this controller does not own; {AUDIT}"
TARGET_TAKEN = f"is already saved by a machine this controller does not own; {AUDIT}"


class Registrations:
    def __init__(self, catalog: Catalog, store: Store, session: str = REMOTE_SESSION) -> None:
        self.catalog, self.store, self.session = catalog, store, session
        self.drift: Counter = Counter()

    def _collision(self, endpoint: Endpoint, machines: Iterable[Machine], keep: str | None) -> None:
        others = [machine for machine in machines if machine.profile_id != keep]
        if any(machine.label == endpoint.name for machine in others):
            self.drift["collision"] += 1
            raise EndpointRefused("label_collision", f"{endpoint.name} {LABEL_TAKEN}")
        if keep is None and any(machine.target == endpoint.target for machine in others):
            self.drift["collision"] += 1
            raise EndpointRefused("target_collision", f"{endpoint.name} {TARGET_TAKEN}")

    def _record(self, endpoint: Endpoint, profile_id: str | None) -> Record:
        scope = endpoint.scope
        return Record(scope.execution_id, scope.generation, endpoint.name, endpoint.target, self.session, profile_id)

    def _add(self, endpoint: Endpoint, records: dict[str, Record], machines: tuple[Machine, ...]) -> str:
        self._collision(endpoint, machines, None)
        execution_id = endpoint.scope.execution_id
        before = tuple(sorted(machine.profile_id for machine in machines))
        records[execution_id] = replace(self._record(endpoint, None), before=before)
        self.store.save(records)
        try:
            return self.catalog.add(endpoint.name, endpoint.target, self.session)
        except EndpointRefused as refused:
            if refused.reason == "machine_refused":
                del records[execution_id]
                self.store.save(records)
            raise

    def _claim(self, endpoint: Endpoint, records: dict[str, Record], machines: tuple[Machine, ...]) -> str:
        record = records.get(endpoint.scope.execution_id)
        if record is not None and endpoint.scope.generation < record.generation:
            raise EndpointRefused(
                "stale_generation", f"{endpoint.name} is registered for generation {record.generation}"
            )
        saved = owned(record, machines)
        if saved is not None and (saved.target, saved.session) != (endpoint.target, self.session):
            self.drift["moved"] += 1
            self.catalog.remove(saved.profile_id)
            machines = tuple(machine for machine in machines if machine.profile_id != saved.profile_id)
            return self._add(endpoint, records, machines)
        if saved is None:
            self.drift["missing"] += 1
            return self._add(endpoint, records, machines)
        if record.profile_id is None:
            self.drift["interrupted"] += 1
        if saved.label != endpoint.name:
            self._collision(endpoint, machines, saved.profile_id)
            self.drift["label"] += 1
            self.catalog.rename(saved.profile_id, endpoint.name)
        return saved.profile_id

    def _register(self, endpoint: Endpoint, records: dict[str, Record], machines: tuple[Machine, ...]) -> str:
        if not endpoint.scope.actor.startswith(ACTORS):
            raise EndpointRefused("unauthorized", f"{endpoint.name} has no launch grant or operator scope")
        profile_id = self._claim(endpoint, records, machines)
        records[endpoint.scope.execution_id] = self._record(endpoint, profile_id)
        self.store.save(records)
        return profile_id

    def register(self, endpoint: Endpoint) -> str:
        with self.store.locked():
            return self._register(endpoint, self.store.load(), self.catalog.machines())

    def _drop(self, records: dict[str, Record], execution_id: str, profile_id: str | None) -> None:
        if profile_id is not None:
            self.catalog.remove(profile_id)
        del records[execution_id]
        self.store.save(records)

    def _retire(self, records: dict[str, Record], machines: tuple[Machine, ...], execution_id: str) -> None:
        saved = owned(records[execution_id], machines)
        if saved is not None:
            self.drift["retired"] += 1
        self._drop(records, execution_id, saved.profile_id if saved else None)

    def recorded(self, endpoint: Endpoint) -> str | None:
        with self.store.locked():
            record = self.store.load().get(endpoint.scope.execution_id)
        return None if record is None else record.profile_id

    def withdraw(self, endpoint: Endpoint) -> None:
        with self.store.locked():
            records = self.store.load()
            if endpoint.scope.execution_id in records:
                self._retire(records, self.catalog.machines(), endpoint.scope.execution_id)

    def _admit(
        self, endpoint: Endpoint, records: dict[str, Record], machines: tuple[Machine, ...], qualifier: Qualifier
    ) -> tuple[str, bool]:
        record = records.get(endpoint.scope.execution_id)
        recorded = None if record is None else record.profile_id
        profile_id = self._register(endpoint, records, machines)
        try:
            verdict = qualifier.verdict(endpoint.name, endpoint.incarnation)
        except Unreachable:
            if profile_id == recorded:
                return profile_id, False
            reason = "unreachable"
        except Incompatible:
            reason = "incompatible"
        else:
            reason = "" if verdict.compatible else "incompatible"
        if reason:
            self._drop(records, endpoint.scope.execution_id, profile_id)
            raise EndpointRefused(reason, f"{endpoint.name} did not qualify: {reason}")
        return profile_id, True

    def reconcile(self, desired: Iterable[Endpoint], qualifier: Qualifier) -> Report:
        with self.store.locked():
            return self._reconcile(newest(desired), self.store.load(), self.catalog.machines(), qualifier)

    def _reconcile(
        self,
        wanted: dict[str, Endpoint],
        records: dict[str, Record],
        machines: tuple[Machine, ...],
        qualifier: Qualifier,
    ) -> Report:
        before = Counter(self.drift)
        report = Report()
        for execution_id in sorted(wanted):
            try:
                profile_id, verified = self._admit(wanted[execution_id], records, machines, qualifier)
                report.registered[execution_id] = profile_id
                if not verified:
                    report.unverified.append(execution_id)
            except EndpointRefused as refused:
                report.refused[execution_id] = refused.reason
        for execution_id in sorted(set(records) - set(wanted)):
            try:
                self._retire(records, machines, execution_id)
                report.retired.append(execution_id)
            except EndpointRefused as refused:
                report.refused[execution_id] = refused.reason
        report.drift = dict(self.drift - before)
        return report
