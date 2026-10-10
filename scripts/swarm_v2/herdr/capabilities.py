import json
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

PROTOCOL = 22
SERVER_CAPABILITIES = {
    "endpoint_protocol_generation": 1,
    "surface_interest": True,
    "health_check": True,
    "detached_server_daemon": True,
}
CLIENT_CAPABILITIES = {"endpoint_protocol_generation": 1, "remote_host_bridge": True}
OPERATIONS = {
    "workspace_create": (("workspace", "create"), "workspace.create"),
    "workspace_list": (("workspace", "list"), "workspace.list"),
    "workspace_close": (("workspace", "close"), "workspace.close"),
    "tab_create": (("tab", "create"), "tab.create"),
    "pane_launch": (("pane", "split"), "pane.split"),
    "pane_run": (("pane", "run"), "pane.send_input"),
    "pane_send_text": (("pane", "send-text"), "pane.send_text"),
    "pane_send_keys": (("pane", "send-keys"), "pane.send_keys"),
    "pane_wait_output": (("pane", "wait-output"), "pane.wait_for_output"),
    "pane_read": (("pane", "read"), "pane.read"),
    "pane_process_info": (("pane", "process-info"), "pane.process_info"),
    "pane_list": (("pane", "list"), "pane.list"),
    "pane_close": (("pane", "close"), "pane.close"),
    "agent_list": (("agent", "list"), "agent.list"),
    "agent_get": (("agent", "get"), "agent.get"),
    "agent_prompt": (("agent", "prompt"), "agent.prompt"),
    "agent_rename": (("agent", "rename"), "agent.rename"),
}
NOT_FORWARDED = {
    "update": "local installation is not forwarded",
    "server_replace": "machine forwarding never installs, starts or restarts a server",
    "session": "session management is not forwarded",
    "agent_attach": "interactive attachment is not forwarded",
    "local_fallback": "a machine command never falls back to the local server",
}
STATUS_SERVER = ("status", "server", "--json")
STATUS_CLIENT = ("status", "client", "--json")
SCHEMA = ("api", "schema", "--json")
PROBE_SECONDS = 30


class Incompatible(RuntimeError):
    pass


class Unreachable(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    client: dict
    server: dict
    methods: frozenset[str]


@dataclass(frozen=True)
class Verdict:
    target: str
    incarnation: str
    refusals: tuple[str, ...]
    matrix: dict[str, str]

    @property
    def compatible(self) -> bool:
        return not self.refusals


def _lacking(found: dict, wanted: dict, side: str) -> list[str]:
    return [
        f"herdr {side} lacks {name}={json.dumps(value)}"
        for name, value in wanted.items()
        if type(found.get(name)) is not type(value) or found.get(name) != value
    ]


def refusals(observed: Observation) -> list[str]:
    found = [] if observed.server.get("running") is True else ["herdr server is not running"]
    if observed.client.get("protocol") != PROTOCOL or observed.server.get("protocol") != PROTOCOL:
        found.append(f"herdr protocol is not {PROTOCOL}")
    if observed.server.get("version") != observed.client.get("version"):
        found.append(
            f"herdr server {observed.server.get('version')} is not the client build "
            f"{observed.client.get('version')}, so its socket methods are unverified"
        )
    found += _lacking(observed.client, CLIENT_CAPABILITIES, "client")
    return found + _lacking(observed.server.get("capabilities") or {}, SERVER_CAPABILITIES, "server")


def matrix(methods: frozenset[str]) -> dict[str, str]:
    table = {
        name: "supported" if method in methods else f"unsupported: server lacks {method}"
        for name, (_, method) in OPERATIONS.items()
    }
    return table | {name: f"unsupported: {reason}" for name, reason in NOT_FORWARDED.items()}


def qualify(target: str, incarnation: str, observed: Observation) -> Verdict:
    return Verdict(target, incarnation, tuple(refusals(observed)), matrix(observed.methods))


def _json(run: Callable[[list[str]], subprocess.CompletedProcess], command: list[str]) -> dict:
    label = " ".join(command[:4])
    try:
        done = run(command)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unreachable(f"{label}: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise Unreachable(f"{label}: exit {done.returncode}")
    try:
        document = json.loads(done.stdout)
    except ValueError as exc:
        raise Unreachable(f"{label}: unreadable reply") from exc
    return document if isinstance(document, dict) else {}


def _schema_methods(schema: dict) -> frozenset[str]:
    requests = schema.get("schemas", {}).get("request", {}).get("oneOf", [])
    found = (request.get("properties", {}).get("method", {}).get("const") for request in requests)
    return frozenset(name for name in found if isinstance(name, str))


def machine_probe(run: Callable[[list[str]], subprocess.CompletedProcess]) -> Callable[[str], Observation]:
    def probe(target: str) -> Observation:
        server = _json(run, ["herdr", "--machine", target, *STATUS_SERVER])
        client = _json(run, ["herdr", *STATUS_CLIENT])
        same = server.get("version") == client.get("version") and server.get("protocol") == client.get("protocol")
        methods = _schema_methods(_json(run, ["herdr", *SCHEMA])) if same else frozenset()
        return Observation(client, server, methods)

    return probe


def runner(environ: dict[str, str]) -> Callable[[list[str]], subprocess.CompletedProcess]:
    def run(command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            command, env=environ, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_SECONDS
        )

    return run


class Qualifier:
    """The incarnation is the caller's execution attempt id: herdr reports none, and the supervisor
    starts one herdr server per attempt and ends the attempt when that server exits."""

    def __init__(self, probe: Callable[[str], Observation]) -> None:
        self.probe = probe
        self.verdicts: dict[str, Verdict] = {}
        self.retired: dict[str, set[str]] = {}
        self.mismatches: Counter = Counter()

    def verdict(self, target: str, incarnation: str) -> Verdict:
        if not target or target.startswith("-") or not incarnation:
            raise Incompatible("a remote herdr operation needs a target and a confirmed server incarnation")
        if incarnation in self.retired.get(target, ()):
            raise Incompatible(f"herdr server incarnation {incarnation} on {target} was replaced")
        cached = self.verdicts.get(target)
        if cached is not None and cached.incarnation == incarnation:
            return cached
        fresh = qualify(target, incarnation, self.probe(target))
        if cached is not None:
            self.retired.setdefault(target, set()).add(cached.incarnation)
        self.verdicts[target] = fresh
        return fresh

    def require(self, target: str, incarnation: str, operation: str) -> list[str]:
        verdict = self.verdict(target, incarnation)
        state = verdict.matrix.get(operation, "unsupported: unknown operation")
        reasons = list(verdict.refusals) + ([] if state == "supported" else [f"{operation} {state}"])
        if reasons:
            for reason in reasons:
                self.mismatches[(target, reason)] += 1
            raise Incompatible("; ".join(reasons))
        return ["herdr", "--machine", target, *OPERATIONS[operation][0]]

    def herdr_capability_mismatch_total(self) -> int:
        return sum(self.mismatches.values())
