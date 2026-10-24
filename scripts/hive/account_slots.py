import hashlib
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from scripts.events import subjects
from scripts.events.bus import Bus, StateConflict
from scripts.events.config import SWARMS_ENV
from scripts.hive import presence

TTL_MS = presence.TTL_MS
LIVE = frozenset({"working", "running", "idle", "waiting", "blocked"})
ENDED = frozenset({"exited", "dead"})


HIVE_ENV = "AGENTIHOOKS_SLOT_HIVE"
EXECUTION_ENV = "AGENTIHOOKS_SLOT_EXECUTION"
UNBOUND = "hive"
REQUEST_TIMEOUT_MS = 10_000


class Unavailable(RuntimeError):
    pass


def key(harness: str, account: str) -> str:
    return hashlib.sha256(json.dumps([harness, account]).encode()).hexdigest()


def _change(state, name, edit):
    for _ in range(4):
        entry = state.get(name)
        value = json.loads(entry.value) if entry else {}
        result = edit(value)
        encoded = json.dumps(value).encode()
        try:
            state.update(name, encoded, entry.revision) if entry else state.create(name, encoded)
            return result
        except StateConflict:
            continue
    raise StateConflict


def _live(value, now):
    for field in ("holders", "hives"):
        value[field] = {name: row for name, row in value.get(field, {}).items() if row["expires_ms"] > now}


def _used(value):
    holders = value["holders"]
    manual = sum(max(row["sessions"] - len(row["observed"]), 0) for row in value["hives"].values())
    return len(holders) + manual


class Slots:
    def __init__(
        self, bus: Bus, hive: str, read: Callable[[], dict], clock: Callable[[], int] = presence.now_ms
    ) -> None:
        self.bus, self.hive, self.read, self.clock = bus, hive, read, clock

    def _accounts(self, slots):
        def remember(value):
            accounts = {tuple(item) for item in value.get("accounts", [])}
            accounts.update((item["harness"], item["account"]) for item in slots)
            value["accounts"] = sorted(accounts)
            return value["accounts"]

        return _change(self.bus.state("hive-slot-accounts"), key("hive", self.hive), remember)

    def heartbeat(self, report: dict, executions: dict) -> None:
        slots = {(item["harness"], item["account"]): item for item in report["slots"]}
        for harness, account in self._accounts(report["slots"]):

            def renew(value):
                now = self.clock()
                _live(value, now)
                owned = {name: row for name, row in value["holders"].items() if row["hive"] == self.hive}
                for execution, row in owned.items():
                    state = executions.get(execution, {}).get("pane", {}).get("state")
                    if state in ENDED:
                        del value["holders"][execution]
                    elif state in LIVE:
                        row["expires_ms"] = now + TTL_MS
                observed = [
                    name
                    for name in report["sessions"].get("_executions", {}).get(f"{harness}:{account}", [])
                    if name in value["holders"] and value["holders"][name]["hive"] == self.hive
                ]
                slot = slots.get((harness, account))
                if slot is not None or any(row["hive"] == self.hive for row in value["holders"].values()):
                    value["hives"][self.hive] = {
                        "cap": slot["cap"] if slot is not None else 0,
                        "sessions": report["sessions"].get(f"{harness}:{account}", 0),
                        "observed": observed,
                        "expires_ms": now + TTL_MS,
                    }
                else:
                    value["hives"].pop(self.hive, None)

            _change(self.bus.state("account-slots"), key(harness, account), renew)

    def take(self, execution: str, harness: str, preferred: str = "") -> str | None:
        report = self.read()
        self.heartbeat(report, {})
        for slot in report["slots"]:
            if slot["harness"] != harness or (preferred and slot["account"] != preferred):
                continue

            def reserve(value):
                now = self.clock()
                _live(value, now)
                holder = value["holders"].get(execution)
                if holder:
                    return holder["hive"] == self.hive
                cap = min(row["cap"] for row in value["hives"].values())
                if _used(value) >= cap:
                    return False
                value["holders"][execution] = {"hive": self.hive, "expires_ms": now + TTL_MS}
                return True

            if _change(self.bus.state("account-slots"), key(harness, slot["account"]), reserve):
                return slot["account"]
        return None

    def release(self, execution: str, harness: str, account: str) -> None:
        def remove(value):
            holders = value.get("holders", {})
            if holders.get(execution, {}).get("hive") == self.hive:
                del holders[execution]

        _change(self.bus.state("account-slots"), key(harness, account), remove)

    def launch(self, execution: str, harness: str, preferred: str, invoke: Callable[[str], Any]) -> Any:
        try:
            account = self.take(execution, harness, preferred)
        except Exception as exc:
            raise Unavailable("Account slot admission failed") from exc
        if account is None:
            raise Unavailable("No account slot available")
        try:
            return invoke(account)
        except Exception as exc:
            if getattr(exc, "status", "") != "ambiguous":
                self.release(execution, harness, account)
            raise


def request_subject(swarm: str, hive: str) -> str:
    return subjects.slot_request(swarm, hive)


def asker(bus: Any, swarm: str, hive: str, timeout_ms: int = REQUEST_TIMEOUT_MS) -> Callable[[dict], Any]:
    def ask(request: dict) -> Any:
        answer = json.loads(bus.request(request_subject(swarm, hive), json.dumps(request).encode(), timeout_ms))
        if not answer["ok"]:
            raise Unavailable(answer["error"])
        return answer["result"]

    return ask


class Remote(Slots):
    def __init__(self, ask: Callable[[dict], Any], hive: str, read: Callable[[], dict] | None = None) -> None:
        self.ask, self.hive, self.read = ask, hive, read

    def heartbeat(self, report: dict, executions: dict) -> None:
        self.ask({"op": "heartbeat", "hive": self.hive, "report": report, "executions": executions})

    def take(self, execution: str, harness: str, preferred: str = "") -> str | None:
        request = {"execution": execution, "harness": harness, "preferred": preferred, "report": self.read()}
        return self.ask({"op": "take", "hive": self.hive, **request})

    def release(self, execution: str, harness: str, account: str) -> None:
        self.ask({"op": "release", "hive": self.hive, "execution": execution, "harness": harness, "account": account})


def _swarm(environ: Mapping[str, str]) -> str:
    return next((swarm for swarm in environ.get(SWARMS_ENV, "").split(",") if swarm), UNBOUND)


def _slots(bus: Bus, backend: str, hive: str, read: Callable[[], dict], environ: Mapping[str, str]) -> Slots:
    if backend == "memory":
        return Slots(bus, hive, read)
    return Remote(asker(bus, _swarm(environ), hive), hive, read)


def _reading(hive: str) -> dict:
    from hooks.context import account_sessions
    from scripts.hive import daemon
    from scripts.swarm.store import connect

    processes = account_sessions.local_processes()
    report = daemon.observe(processes=processes)
    report["sessions"]["_executions"] = daemon._executions(connect(), hive, processes)
    return report


def execute(harness: str, account: str, environ: Mapping[str, str], invoke: Callable[[dict[str, str]], Any]) -> Any:
    from scripts.events.config import BusConfig
    from scripts.events.factory import open_bus

    child = {name: value for name, value in environ.items() if name not in (HIVE_ENV, EXECUTION_ENV)}
    hive = environ.get(HIVE_ENV)
    if not hive:
        return invoke(child)
    config = BusConfig.from_environ()
    bus = open_bus(config)
    try:
        slots = _slots(bus, config.backend, hive, lambda: _reading(hive), environ)
        return slots.launch(environ[EXECUTION_ENV], harness, account, lambda selected: invoke(child))
    finally:
        bus.close()


def release(hive: str, execution: str, harness: str, account: str) -> None:
    _remote(hive, lambda slots: slots.release(execution, harness, account))


def _remote(hive: str, act: Callable[["Remote"], None]) -> None:
    from scripts.events.config import BusConfig
    from scripts.events.factory import open_bus

    config = BusConfig.from_environ()
    if config.backend == "memory":
        return
    bus = open_bus(config)
    try:
        act(Remote(asker(bus, _swarm(os.environ), hive), hive))
    finally:
        bus.close()


def publish(hive: str, report: dict, hosted: dict) -> None:
    _remote(hive, lambda slots: slots.heartbeat(report, hosted["executions"]))
