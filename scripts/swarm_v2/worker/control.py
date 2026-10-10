import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from scripts.swarm_v2.runtime.operations import digest

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


def _voided(record: dict) -> bool:
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
        self, transport: CommandTransport, state_path: Path, handlers: Mapping[str, Callable[[dict], dict]]
    ) -> None:
        self.transport, self.path, self.handlers = transport, state_path, handlers
        self.records = json.loads(state_path.read_bytes()) if state_path.exists() else {}

    def may_mutate(self) -> bool:
        return not any(record["kind"] == DRAIN and not _voided(record) for record in self.records.values())

    def step(self) -> None:
        for command_id in list(self.records):
            self._advance(command_id)
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

    def checkpointed(self, checkpoint: str) -> None:
        for command_id, record in self.records.items():
            if record["kind"] == DRAIN and record["state"] in (RECEIVED, ACCEPTED) and record["outcome"] is None:
                record["outcome"] = {"status": "checkpointed", "checkpoint": checkpoint}
                self._save()
                self._advance(command_id)

    def _receive(self, command: dict) -> None:
        record = {
            "kind": command["kind"],
            "payload_digest": command["payload_digest"],
            "payload": command["payload"],
            "state": RECEIVED,
            "outcome": None,
        }
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
