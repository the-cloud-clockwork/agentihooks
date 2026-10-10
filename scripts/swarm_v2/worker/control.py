import base64
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from scripts.swarm_v2.runtime.operations import digest

INBOX = Path("/home/worker/commands/inbox")
STATE = Path("/home/worker/commands/state.json")
LAUNCH = Path("/var/run/swarm/launch/launch.json")
ENVELOPE = ("command_id", "execution_id", "generation", "kind", "payload", "payload_digest", "expires_at_ms")
COMMAND_ID = re.compile(r"cmd-[0-9a-f]{32}")
ENCODED = re.compile(r"[A-Za-z0-9_-]+={0,2}")
MODES = ("status", "deliver")
USAGE = "usage: status ENVELOPE | deliver ENVELOPE"

DRAIN, ANSWER = "drain", "answer"
RECEIVED, ACCEPTED, RUNNING, DONE, REPORTED, REJECTED = (
    "received",
    "accepted",
    "running",
    "done",
    "reported",
    "rejected",
)
UNREACHABLE = (ConnectionError, TimeoutError)


def _entry(command: Mapping, state: str) -> dict:
    return {
        "kind": command["kind"],
        "payload_digest": command["payload_digest"],
        "payload": command["payload"],
        "state": state,
        "outcome": None,
    }


def _voided(record: dict) -> bool:
    if record.get("inbox") and record["state"] == RECEIVED:
        return True
    return record["state"] == REJECTED and record.get("refusal") == "expired" and record["outcome"] is None


class CommandRefused(Exception):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


class CommandTransport(Protocol):
    def poll(self) -> list[dict]: ...

    def ack(self, command_id: str, payload_digest: str) -> dict: ...

    def complete(self, command_id: str, outcome: dict) -> dict: ...


class RouteTransport:
    """Speaks the worker command endpoints through `send(method, path, body) -> (status, reply)`."""

    def __init__(self, execution_id: str, send: Callable[[str, str, dict | None], tuple[int, dict]]) -> None:
        self.base, self.send = f"/v2/executions/{execution_id}/commands", send

    def poll(self) -> list[dict]:
        return self._call("GET", self.base, None)["commands"]

    def ack(self, command_id: str, payload_digest: str) -> dict:
        return self._call("POST", f"{self.base}/{command_id}/ack", {"payload_digest": payload_digest})

    def complete(self, command_id: str, outcome: dict) -> dict:
        return self._call("POST", f"{self.base}/{command_id}/complete", {"outcome": outcome})

    def _call(self, method: str, path: str, body: dict | None) -> dict:
        status, reply = self.send(method, path, body)
        if status == 200:
            return reply
        if reply["retry"] == "same_request":
            raise ConnectionError(reply["message"])
        raise CommandRefused(reply["error_class"], reply["message"])


class WorkerControl:
    """Runs a command only after the server accepted it and never twice; `state_path` must outlive the process."""

    def __init__(
        self,
        transport: CommandTransport,
        state_path: Path,
        handlers: Mapping[str, Callable[[dict], dict]],
        inbox: Path | None = None,
    ) -> None:
        self.transport, self.path, self.handlers, self.inbox = transport, state_path, handlers, inbox
        self.records = json.loads(state_path.read_bytes()) if state_path.exists() else {}

    def may_mutate(self) -> bool:
        return not any(record["kind"] == DRAIN and not _voided(record) for record in self.records.values())

    def step(self) -> None:
        for command_id in list(self.records):
            self._advance(command_id)
        if self.inbox is not None:
            for path in sorted(self.inbox.glob("cmd-*.json")):
                self._collect(path)
        try:
            commands = self.transport.poll()
        except UNREACHABLE:
            return
        for command in commands:
            known = command["command_id"] in self.records
            if (
                not known
                and digest({"kind": command["kind"], "payload": command["payload"]}) == command["payload_digest"]
            ):
                self._receive(command)

    def _collect(self, path: Path) -> None:
        """An inbox command binds the worker only once the server accepts it, so a forged file never drains it."""
        try:
            command = checked(json.loads(path.read_bytes()))
        except OSError:
            return
        except ValueError:
            path.unlink()
            return
        command_id = command["command_id"]
        known = command_id in self.records
        if not known:
            self.records[command_id] = {**_entry(command, RECEIVED), "inbox": True}
            self._save()
        path.unlink()
        if not known:
            self._advance(command_id)

    def checkpointed(self, checkpoint: str) -> None:
        for command_id, record in self.records.items():
            if record["kind"] == DRAIN and record["state"] in (RECEIVED, ACCEPTED) and record["outcome"] is None:
                record["outcome"] = {"status": "checkpointed", "checkpoint": checkpoint}
                self._save()
                self._advance(command_id)

    def _receive(self, command: dict) -> None:
        record = _entry(command, RECEIVED)
        if command["state"] == "accepted":
            record["state"] = ACCEPTED
            if command["kind"] != DRAIN:
                record["state"] = DONE
                record["outcome"] = {
                    "status": "not_run",
                    "detail": "accepted before this worker state existed; not rerun",
                }
        self.records[command["command_id"]] = record
        self._save()
        self._advance(command["command_id"])

    def _advance(self, command_id: str) -> None:
        record = self.records[command_id]
        try:
            if record["state"] == RUNNING:
                self._settle(record, {"status": "failed", "detail": "interrupted while running; not rerun"})
            if record["state"] == RECEIVED:
                self.transport.ack(command_id, record["payload_digest"])
                self._move(record, ACCEPTED)
            if record["state"] == ACCEPTED and record["kind"] == ANSWER and not self.may_mutate():
                self._settle(record, {"status": "not_run", "detail": "refused while draining"})
            if record["state"] == ACCEPTED and record["kind"] != DRAIN:
                self._move(record, RUNNING)
                self._settle(record, self._run(record))
            if record["state"] == ACCEPTED and record["outcome"] is not None:
                self._move(record, DONE)
            if record["state"] == DONE:
                self.transport.complete(command_id, record["outcome"])
                self._move(record, REPORTED)
        except UNREACHABLE:
            return
        except CommandRefused as error:
            if record.get("inbox") and record["state"] == RECEIVED:
                del self.records[command_id]
                self._save()
                return
            record["refusal"] = error.error_class
            self._move(record, REJECTED)

    def _run(self, record: dict) -> dict:
        handler = self.handlers.get(record["kind"])
        if handler is None:
            return {"status": "failed", "detail": "no handler for this command kind"}
        try:
            return handler(record["payload"])
        except Exception as error:
            return {"status": "failed", "detail": f"handler raised {type(error).__name__}"}

    def _settle(self, record: dict, outcome: dict) -> None:
        record["outcome"] = outcome
        self._move(record, DONE)

    def _move(self, record: dict, state: str) -> None:
        record["state"] = state
        self._save()

    def _save(self) -> None:
        staged = self.path.with_name(f"{self.path.name}.tmp")
        with staged.open("wb") as handle:
            handle.write(json.dumps(self.records).encode())
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(self.path)


def encode(record: Mapping) -> str:
    envelope = {name: record[name] for name in ENVELOPE}
    return base64.urlsafe_b64encode(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).decode()


def checked(envelope: object) -> dict:
    if not isinstance(envelope, dict) or sorted(envelope) != sorted(ENVELOPE):
        raise ValueError("the envelope fields are wrong")
    if not isinstance(envelope["command_id"], str) or not COMMAND_ID.fullmatch(envelope["command_id"]):
        raise ValueError("the command id is malformed")
    if type(envelope["generation"]) is not int or type(envelope["expires_at_ms"]) is not int:
        raise ValueError("the generation and expiry must be integers")
    if digest({"kind": envelope["kind"], "payload": envelope["payload"]}) != envelope["payload_digest"]:
        raise ValueError("the payload digest does not match")
    return envelope


def decode(text: str) -> dict:
    if not ENCODED.fullmatch(text):
        raise ValueError("the envelope is not url safe base64")
    return checked(json.loads(base64.urlsafe_b64decode(text)))


def status(command_id: str, inbox: Path, state: Path) -> str:
    if (inbox / f"{command_id}.json").exists():
        return "queued"
    return "known" if state.exists() and command_id in json.loads(state.read_bytes()) else "absent"


def _refused(envelope: Mapping, reason: str) -> dict:
    return {
        "command_id": envelope.get("command_id", ""),
        "execution_id": envelope.get("execution_id", ""),
        "generation": envelope.get("generation", 0),
        "state": "refused",
        "reason": reason,
    }


def handle(mode: str, text: str, inbox: Path, state: Path, launch: Path, now_ms: int) -> tuple[int, dict]:
    try:
        envelope = decode(text)
    except ValueError as error:
        return 2, _refused({}, str(error))
    try:
        identity = json.loads(launch.read_bytes())
        owner = (identity["execution_id"], identity["generation"])
    except (OSError, ValueError, KeyError, TypeError):
        return 2, _refused(envelope, "the launch record is unreadable")
    reply = {"command_id": envelope["command_id"], "execution_id": owner[0], "generation": owner[1]}
    if (envelope["execution_id"], envelope["generation"]) != owner:
        return 2, _refused(reply, "the envelope names another execution")
    held = status(envelope["command_id"], inbox, state)
    if mode == "status":
        return 0, {**reply, "state": held}
    if held != "absent":
        return 0, {**reply, "state": "known"}
    if now_ms >= envelope["expires_at_ms"]:
        return 2, _refused(reply, "the command expired")
    inbox.mkdir(parents=True, exist_ok=True)
    staged = inbox / f"{envelope['command_id']}.{os.getpid()}.tmp"
    with staged.open("wb") as stream:
        stream.write(json.dumps(envelope).encode())
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(staged, inbox / f"{envelope['command_id']}.json")
    except FileExistsError:
        return 0, {**reply, "state": "known"}
    finally:
        staged.unlink()
    return 0, {**reply, "state": "queued"}


def main(
    argv: list[str] | None = None,
    inbox: Path = INBOX,
    state: Path = STATE,
    launch: Path = LAUNCH,
    clock: Callable[[], float] | None = None,
) -> int:
    """The only command a controller may exec in a worker Pod: one mode and one encoded envelope, no shell."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in MODES:
        code, reply = 2, _refused({}, USAGE)
    else:
        code, reply = handle(args[0], args[1], inbox, state, launch, int((clock or time.time)() * 1000))
    print(json.dumps(reply, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
