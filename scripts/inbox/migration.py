import argparse
import hashlib
import json
import os
import uuid
from pathlib import Path

from scripts.events.bus import Bus, StateConflict
from scripts.events.config import BusConfig
from scripts.events.factory import open_bus
from scripts.inbox.bus import Message, SeatInbox
from scripts.inbox.store import CLOSED, PREFIX, STATES, _item

BUCKET = "inbox_legacy"


def _encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def snapshot(client) -> list[dict]:
    root = PREFIX.removesuffix(":inbox")
    keys = set(client.scan_iter(match=f"{PREFIX}:item:*"))
    keys.update(client.scan_iter(match=f"{root}:hive:*:inbox:item:*"))
    records = []
    for key in sorted(keys):
        namespace, item_id = key.rsplit(":inbox:item:", 1)
        with client.pipeline() as pipe:
            pipe.hgetall(key)
            pipe.lrange(f"{namespace}:inbox:history:{item_id}", 0, -1)
            pipe.get(f"{namespace}:inbox:entry:{item_id}")
            raw, history, entry = pipe.execute()
        if not raw or raw["state"] in CLOSED:
            continue
        records.append(
            {
                "world": "" if namespace == root else namespace.removeprefix(f"{root}:hive:"),
                "item": raw,
                "history": history,
                "entry": entry,
            }
        )
    return records


def _key(record: dict) -> str:
    return digest([record["world"], record["item"]["id"]])


def _message(record: dict) -> tuple[str, Message]:
    item = _item(record["item"], record["item"]["id"])
    if item.state not in STATES or item.state in CLOSED:
        raise ValueError("migration requires an open inbox item")
    return item.address, Message(item.id, item.sender, item.text, item.ref, item.fyi, item.task)


def _report(records: list[dict]) -> dict:
    return {
        "items": len(records),
        "history_entries": sum(len(record["history"]) for record in records),
        "digest": digest(records),
    }


def read_records(records: list[dict], bus: Bus) -> list[dict]:
    state = bus.state(BUCKET)
    found = []
    for record in records:
        entry = state.get(_key(record))
        if entry is None or entry.value != _encode(record):
            raise ValueError("migration verification failed")
        found.append(json.loads(entry.value))
    return found


def import_snapshot(records: list[dict], bus: Bus) -> dict:
    messages = [_message(record) for record in records]
    ids = [message.id for _, message in messages]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate item id in migration snapshot")
    state = bus.state(BUCKET)
    for record, (address, message) in zip(records, messages):
        key, value = _key(record), _encode(record)
        try:
            state.create(key, value)
        except StateConflict:
            if state.get(key).value != value:
                raise ValueError("item already has a different migration record") from None
        SeatInbox(bus, address.partition("@")[2] or "standalone").send(address, message)
    if read_records(records, bus) != records:
        raise ValueError("migration verification failed")
    return _report(records)


def _destination(bus: Bus) -> str:
    state = bus.state(BUCKET)
    value = uuid.uuid4().hex.encode()
    try:
        state.create("destination", value)
    except StateConflict:
        value = state.get("destination").value
    return value.decode()


def rehearse(records: list[dict], bus: Bus, destination: str) -> dict:
    copied = json.loads(_encode(records))
    return {"version": 1, "copy": digest(destination), "store": _destination(bus), **import_snapshot(copied, bus)}


def apply(client, bus: Bus, proof: dict, destination: str, *, enabled: bool = False) -> dict:
    if not enabled:
        raise ValueError("live inbox migration is disabled")
    if proof["version"] != 1:
        raise ValueError("unsupported migration proof version")
    if proof["copy"] == digest(destination):
        raise ValueError("live migration requires a separate copy destination")
    records = snapshot(client)
    if _report(records) != {key: proof[key] for key in ("items", "history_entries", "digest")}:
        raise ValueError("source changed since rehearsal")
    if proof["store"] == _destination(bus):
        raise ValueError("live migration requires a separate copy destination")
    report = import_snapshot(records, bus)
    if snapshot(client) != records:
        raise ValueError("source changed during migration; cutover is unverified")
    return report


def _source():
    from scripts.swarm.store import redis_client

    return redis_client()


def _write(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy and rehearse open Redis inbox items before a scheduled NATS cutover"
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    export = modes.add_parser("snapshot")
    export.add_argument("output", type=Path)
    rehearsal = modes.add_parser("rehearse")
    rehearsal.add_argument("snapshot", type=Path)
    rehearsal.add_argument("--receipt", required=True, type=Path)
    live = modes.add_parser("apply")
    live.add_argument("receipt", type=Path)
    live.add_argument("--enable-live", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "snapshot":
        records = snapshot(_source())
        _write(args.output, records)
        print(json.dumps(_report(records)))
        return 0
    if args.mode == "apply" and not args.enable_live:
        parser.error("live inbox migration is disabled; a scheduled cutover requires --enable-live")
    config = BusConfig.from_environ()
    if config.backend != "nats" or config.url is None:
        parser.error("migration requires an explicit NATS backend and URL")
    bus = open_bus(config)
    try:
        if args.mode == "rehearse":
            report = rehearse(json.loads(args.snapshot.read_text()), bus, config.url)
            _write(args.receipt, report)
        else:
            report = apply(_source(), bus, json.loads(args.receipt.read_text()), config.url, enabled=args.enable_live)
    finally:
        bus.close()
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
