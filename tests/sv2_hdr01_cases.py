import copy
import hashlib
import json
import subprocess
from pathlib import Path

from scripts.swarm_v2.herdr import capabilities
from scripts.swarm_v2.herdr.capabilities import Incompatible, Qualifier

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("herdr-capabilities.json",)
TARGET = "worker-fixture"
ONE_WINDOW = (
    "workspace_create",
    "pane_launch",
    "pane_run",
    "pane_wait_output",
    "pane_read",
    "pane_send_text",
    "pane_send_keys",
    "pane_close",
    "workspace_close",
)
EVIDENCE_CLASS = (
    "mocked: status and schema replies captured from the pinned herdr 0.9.1 binary over saved machine forwarding, "
    "replayed through a recording command runner; no herdr process or SSH connection runs in these cases"
)


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-capabilities.json").read_text())


class Runner:
    def __init__(self, server: dict, client: dict | None = None) -> None:
        self.fx = fixture()
        self.server = server
        self.client = client or self.fx["client"]
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(list(command))
        replies = {
            ("herdr", "--machine", TARGET, *capabilities.STATUS_SERVER): self.server,
            ("herdr", *capabilities.STATUS_CLIENT): self.client,
            ("herdr", *capabilities.SCHEMA): self.schema(),
        }
        reply = replies.get(tuple(command))
        if reply is None:
            return subprocess.CompletedProcess(command, 2, "", "unexpected command")
        return subprocess.CompletedProcess(command, 0, json.dumps(reply), "")

    def schema(self) -> dict:
        requests = [{"properties": {"method": {"const": name}}} for name in self.fx["methods"]]
        return {"protocol": self.fx["schema_protocol"], "schemas": {"request": {"oneOf": requests}}}


def qualifier(runner: Runner) -> Qualifier:
    return Qualifier(capabilities.machine_probe(runner))


def _refused(action) -> str:
    try:
        action()
    except Incompatible as exc:
        return str(exc)
    return ""


def _mutating(commands: list[list[str]]) -> list[list[str]]:
    reads = {capabilities.STATUS_SERVER, capabilities.STATUS_CLIENT, capabilities.SCHEMA}
    return [command for command in commands if tuple(command[-3:]) not in reads]


def _positive_once() -> dict:
    runner = Runner(fixture()["server"])
    gate = qualifier(runner)
    commands = {name: gate.require(TARGET, "incarnation-1", name) for name in ONE_WINDOW}
    verdict = gate.verdict(TARGET, "incarnation-1")
    return {
        "commands": commands,
        "probe_commands": runner.commands,
        "matrix": verdict.matrix,
        "herdr_capability_mismatch_total": gate.herdr_capability_mismatch_total(),
    }


def _positive() -> tuple[dict, bool]:
    first, second = _positive_once(), _positive_once()
    every_machine = all(argv[:3] == ["herdr", "--machine", TARGET] for argv in first["commands"].values())
    probed_once = len(first["probe_commands"]) == 3
    passed = first == second and every_machine and probed_once and first["herdr_capability_mismatch_total"] == 0
    return {"first": first, "second_identical": first == second}, passed


def _reject(server: dict, operation: str = "workspace_create") -> dict:
    runner = Runner(server)
    gate = qualifier(runner)
    refusal = _refused(lambda: gate.require(TARGET, "incarnation-1", operation))
    again = _refused(lambda: gate.require(TARGET, "incarnation-1", operation))
    return {
        "refusal": refusal,
        "repeat_refusal": again,
        "commands_run": runner.commands,
        "mutating_commands": _mutating(runner.commands),
        "herdr_capability_mismatch_total": gate.herdr_capability_mismatch_total(),
    }


def _rejection() -> tuple[dict, bool]:
    fx = fixture()
    observed = {
        "missing_surface_interest": _reject(fx["missing_forwarding_server"]),
        "session_member_server": _reject(fx["session_member_server"]),
        "unforwarded_update": _reject(fx["server"], "update"),
        "unforwarded_local_fallback": _reject(fx["server"], "local_fallback"),
        "no_target": _refused(lambda: qualifier(Runner(fx["server"])).require("", "incarnation-1", "pane_read")),
    }
    rejected = [observed[name] for name in observed if name != "no_target"]
    passed = (
        all(entry["refusal"] and entry["refusal"] == entry["repeat_refusal"] for entry in rejected)
        and all(not entry["mutating_commands"] for entry in rejected)
        and "surface_interest" in observed["missing_surface_interest"]["refusal"]
        and "detached_server_daemon" in observed["session_member_server"]["refusal"]
        and bool(observed["no_target"])
    )
    return observed, passed


def _recovery() -> tuple[dict, bool]:
    fx = fixture()
    runner = Runner(fx["server"])
    gate = qualifier(runner)
    steps = {"accepted": gate.require(TARGET, "incarnation-1", "pane_read")}
    runner.server = copy.deepcopy(fx["missing_forwarding_server"])
    steps["same_incarnation_cached"] = gate.require(TARGET, "incarnation-1", "pane_read")
    steps["restarted_without_capability"] = _refused(lambda: gate.require(TARGET, "incarnation-2", "pane_read"))
    runner.server = copy.deepcopy(fx["server"])
    steps["restarted_compatible"] = gate.require(TARGET, "incarnation-3", "pane_read")
    steps["stale_incarnation"] = _refused(lambda: gate.require(TARGET, "incarnation-1", "pane_read"))
    steps["probes"] = sum(1 for command in runner.commands if "--machine" in command)
    steps["herdr_capability_mismatch_total"] = gate.herdr_capability_mismatch_total()
    passed = (
        steps["accepted"] == steps["same_incarnation_cached"] == steps["restarted_compatible"]
        and "surface_interest" in steps["restarted_without_capability"]
        and "was replaced" in steps["stale_incarnation"]
        and steps["probes"] == 3
        and steps["herdr_capability_mismatch_total"] == 1
    )
    return steps, passed


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-HDR-01-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
