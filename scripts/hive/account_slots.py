import hashlib
import json
from collections.abc import Callable
from typing import Any

from scripts.events.bus import Bus, StateConflict
from scripts.hive import presence
from scripts.scheduler.process_interval import Interval

TTL_MS = presence.TTL_MS
LIVE = frozenset({"working", "running", "idle", "waiting", "blocked"})
ENDED = frozenset({"exited", "dead"})


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
                observed = [name for name in value["hives"].get(self.hive, {}).get("observed", []) if name in owned]
                for execution, row in owned.items():
                    state = executions.get(execution, {}).get("pane", {}).get("state")
                    if state in ENDED:
                        del value["holders"][execution]
                        observed = [name for name in observed if name != execution]
                    elif state in LIVE:
                        row["expires_ms"] = now + TTL_MS
                        if execution not in observed:
                            observed.append(execution)
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
        interval = Interval(
            lambda: self.heartbeat(self.read(), {execution: {"pane": {"state": "working"}}}),
            every=presence.BEAT_SECONDS,
        )
        try:
            interval.start()
            return invoke(account)
        except Exception as exc:
            if getattr(exc, "status", "") != "ambiguous":
                self.release(execution, harness, account)
            raise
        finally:
            interval.close()


def launch(hive: str, execution: str, argv: list[str], invoke: Callable[[list[str]], Any]) -> Any:
    from scripts.events.config import BusConfig
    from scripts.events.factory import open_bus
    from scripts.hive.daemon import observe

    split = argv.index("--") + 1
    passed = argv[split:]
    route = passed.index("--route") if "--route" in passed else None
    preferred = passed[route + 1] if route is not None else ""
    tail = passed[:route] + passed[route + 2 :] if route is not None else passed
    bus = open_bus(BusConfig.from_environ())
    try:
        slots = Slots(bus, hive, observe)
        return slots.launch(
            execution,
            argv[argv.index("--agent") + 1],
            "unrouted" if preferred == "interactive" else preferred,
            lambda account: invoke(
                [*argv[:split], "--route", "interactive" if account == "unrouted" else account, *tail]
            ),
        )
    finally:
        bus.close()


def publish(hive: str, report: dict, hosted: dict) -> None:
    from scripts.events.config import BusConfig
    from scripts.events.factory import open_bus

    config = BusConfig.from_environ()
    if config.backend == "memory":
        return
    bus = open_bus(config)
    try:
        Slots(bus, hive, lambda: report).heartbeat(report, hosted["executions"])
    finally:
        bus.close()
