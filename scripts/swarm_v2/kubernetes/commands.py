"""Worker commands for Kubernetes executions: the authenticated command endpoint first, one fixed helper as fallback."""

import json
import re
import subprocess
from collections.abc import Callable
from typing import Protocol

from redis.exceptions import RedisError

from hooks.secrets import redact
from scripts.swarm.store import RedisStore
from scripts.swarm_v2.api.commands import ISSUED, CommandQueue
from scripts.swarm_v2.auth_context import GrantRefused
from scripts.swarm_v2.kubernetes.client import ApiRefused, PodApi
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.runtime.commands import Action
from scripts.swarm_v2.runtime.operations import Observation, Operation, Phase, digest
from scripts.swarm_v2.worker.control import encode

HELPER = ("python", "-m", "scripts.swarm_v2.worker.control")
CONTAINER = "agent"
KINDS = {Action.ANSWER: "answer", Action.DRAIN: "drain", Action.CANCEL: "cancel"}
MODES = ("status", "deliver")
REPLIES = frozenset(("queued", "known", "absent", "refused"))
IDENTITY = ("command_id", "execution_id", "generation")
EXEC_TIMEOUT_SECONDS = 10
ENVELOPE_CHARS = 100_000
RAW_BYTES = 65536
REPORT_CHARS = 2048
REPORTS_KEPT = 200
_ESCAPES = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def sanitize_report(raw: bytes) -> str:
    if len(raw) > RAW_BYTES:
        return f"[{len(raw)} bytes withheld]"
    text = _CONTROLS.sub("", _ESCAPES.sub("", raw.decode(errors="replace")))
    text = redact(text, mode="strict")
    return text if len(text) <= REPORT_CHARS else f"{text[:REPORT_CHARS]}[truncated]"


def worker_transport_ambiguous_total(store: RedisStore, slug: str) -> dict[str, int]:
    counts = store.redis.hgetall(store.key(slug, "worker-transport-ambiguous"))
    return {mode: int(counts.get(mode, 0)) for mode in MODES}


def worker_transport_reports(store: RedisStore, slug: str) -> list[dict]:
    return [json.loads(raw) for raw in store.redis.lrange(store.key(slug, "worker-transport-reports"), 0, -1)]


class PodExec(Protocol):
    def run(self, namespace: str, pod: str, argv: tuple[str, ...]) -> tuple[int, bytes, bytes]:
        """Run argv in the agent container without a shell; raise TimeoutError or ConnectionError when unsure."""
        ...


class KubectlExec:
    def __init__(self, kubectl: str = "kubectl", runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.kubectl, self.runner = kubectl, runner

    def run(self, namespace: str, pod: str, argv: tuple[str, ...]) -> tuple[int, bytes, bytes]:
        command = [self.kubectl, "exec", "--namespace", namespace, pod, "--container", CONTAINER, "--", *argv]
        try:
            done = self.runner(command, capture_output=True, timeout=EXEC_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired:
            raise TimeoutError("the helper exec timed out") from None
        except OSError:
            raise ConnectionError("kubectl could not start") from None
        return done.returncode, done.stdout, done.stderr


def _reply(out: bytes, record: dict) -> str | None:
    try:
        reply = json.loads(out)
    except ValueError:
        return None
    if not isinstance(reply, dict) or reply.get("state") not in REPLIES:
        return None
    return reply["state"] if all(reply.get(name) == record[name] for name in IDENTITY) else None


class KubernetesCommandTransport:
    """Commands are issued into the worker command queue; only an issued, unexpired command may take the fallback."""

    backend = BACKEND
    commands = frozenset(KINDS)

    def __init__(
        self, queue: CommandQueue, pods: PodApi, runner: PodExec, expires_in_ms: int, fallback_enabled: bool = True
    ) -> None:
        self.queue, self.pods, self.runner = queue, pods, runner
        self.expires_in_ms, self.fallback_enabled = expires_in_ms, fallback_enabled
        self.owner = owner_for(queue.slug)

    def observe_operation(self, operation: Operation) -> Observation:
        try:
            record = self.queue.outcome(
                operation.execution_id, self.queue.command_id(operation.execution_id, operation.operation_id)
            )
        except RedisError:
            return Observation(Phase.UNKNOWN)
        return Observation(Phase.ABSENT) if record is None else self._observed(operation, record)

    def apply_operation(self, operation: Operation, payload: dict) -> Observation:
        kind = KINDS.get(payload.get("command"))
        text = payload.get("text")
        if kind is None or set(payload) != {"command", "text"} or (kind != "answer" and text != ""):
            return Observation(Phase.REFUSED)
        body = {"text": text} if kind == "answer" else {}
        try:
            record = self.queue.issue(
                operation.execution_id, operation.generation, kind, body, operation.operation_id, self.expires_in_ms
            )
        except GrantRefused as error:
            return Observation(Phase.UNKNOWN if error.error_class == "dependency_unavailable" else Phase.REFUSED)
        except RedisError:
            return Observation(Phase.UNKNOWN)
        return self._observed(operation, record)

    def fallback(self, execution_id: str, command_id: str) -> str:
        if not self.fallback_enabled:
            return "disabled"
        try:
            return self._fallback(execution_id, command_id)
        except (RedisError, ApiRefused, ConnectionError, TimeoutError):
            return "unavailable"

    def _fallback(self, execution_id: str, command_id: str) -> str:
        record = self.queue.outcome(execution_id, command_id)
        if record is None:
            return "absent"
        if record["state"] != ISSUED:
            return record["state"]
        occupants = self.queue.store.execution_occupants(self.queue.slug).values()
        if (execution_id, record["generation"]) not in {(agent.execution_id, agent.generation) for agent in occupants}:
            return "stale"
        envelope = encode(record)
        if len(envelope) > ENVELOPE_CHARS:
            return "too_large"
        if not self._labelled(record):
            return "refused"
        state = self._helper(record, "status", envelope)
        if state == "absent":
            state = self._helper(record, "deliver", envelope)
        return state

    def _observed(self, operation: Operation, record: dict) -> Observation:
        payload = {"command": record["kind"], "text": record["payload"].get("text", "")}
        if (record["execution_id"], record["generation"]) != (operation.execution_id, operation.generation) or digest(
            {"action": operation.action, "payload": payload}
        ) != operation.payload_digest:
            return Observation(Phase.REFUSED)
        result = {name: record[name] for name in ("command_id", "state", "expires_at_ms")}
        return Observation(
            Phase.APPLIED, record["execution_id"], record["generation"], BACKEND, operation.payload_digest, result
        )

    def _labelled(self, record: dict) -> bool:
        pod = self.pods.read_pod(f"swarm-{record['execution_id']}")
        if pod is None or "deletionTimestamp" in pod["metadata"]:
            return False
        labels = pod["metadata"].get("labels", {})
        expected = {OWNER_LABEL: self.owner, EXECUTION_LABEL: record["execution_id"]}
        expected[GENERATION_LABEL] = str(record["generation"])
        return {key: labels.get(key) for key in expected} == expected

    def _helper(self, record: dict, mode: str, envelope: str) -> str:
        pod = f"swarm-{record['execution_id']}"
        try:
            code, out, err = self.runner.run(self.pods.namespace, pod, (*HELPER, mode, envelope))
        except (TimeoutError, ConnectionError):
            code, out, err = None, b"", b""
        self._report(record, mode, code, out, err)
        state = None if code is None else _reply(out, record)
        if state is None or (code == 0) == (state == "refused"):
            store = self.queue.store
            store.redis.hincrby(store.key(self.queue.slug, "worker-transport-ambiguous"), mode)
            return "ambiguous"
        return state

    def _report(self, record: dict, mode: str, code: int | None, out: bytes, err: bytes) -> None:
        store, key = self.queue.store, self.queue.store.key(self.queue.slug, "worker-transport-reports")
        report = {
            "execution_id": record["execution_id"],
            "generation": record["generation"],
            "command_id": record["command_id"],
            "mode": mode,
            "exit": code,
            "stdout": sanitize_report(out),
            "stderr": sanitize_report(err),
        }
        with store.redis.pipeline() as pipe:
            pipe.rpush(key, json.dumps(report))
            pipe.ltrim(key, -REPORTS_KEPT, -1)
            pipe.execute()
