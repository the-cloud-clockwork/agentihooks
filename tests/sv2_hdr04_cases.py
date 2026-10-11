import copy
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from scripts.swarm_v2.herdr.capabilities import Qualifier, machine_probe
from scripts.swarm_v2.herdr.catalog import Catalog
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused
from scripts.swarm_v2.herdr.reconcile import Registrations, Store
from scripts.swarm_v2.herdr.target import DELIVERIES, Router, Target, remote_environ
from tests import sv2_hdr03_cases

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("herdr-pane-routing.json", "herdr-registrations.json", "herdr-capabilities.json")
EVIDENCE_CLASS = (
    "mocked: a recording stand in for the Anton herdr client answers herdr machine commands with the 0.9.1 reply "
    "shapes of SV2-HDR-03 and serves agent get, agent prompt and pane send-text for Local and each saved machine "
    "from the fixture; no herdr process, SSH connection or cluster runs in these cases"
)
PANE_COMMANDS = {("agent", "get"), ("agent", "prompt"), ("pane", "send-text"), ("pane", "read")}


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-pane-routing.json").read_text())


def _server(conversation_id: str) -> dict:
    fx = fixture()
    row = {"pane_id": fx["pane_id"], "name": fx["agent"], "agent_session": {"kind": "id", "value": conversation_id}}
    return {"panes": {fx["pane_id"]: row}, "received": []}


class Fleet(sv2_hdr03_cases.Herdr):
    """The Anton herdr client with Local and two saved worker machines, each serving the same pane id and label."""

    def __init__(self) -> None:
        super().__init__([sv2_hdr03_cases.fixture()["manual"]], set())
        fx = fixture()
        self.local = _server(fx["local"]["conversation_id"])
        self.remote = {execution_id: _server(entry["conversation_id"]) for execution_id, entry in fx["remote"].items()}
        self.ready = {endpoint.target for endpoint in sv2_hdr03_cases.endpoints()}
        self.hosts: dict[str, str] = {}
        self.lost: set[str] = set()

    def _machine_server(self, profile_id: str) -> dict | None:
        machine = next((entry for entry in self.machines if entry["id"] == profile_id), None)
        if machine is None:
            return None
        return self.remote.get(self.hosts.get(machine["target"], ""))

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        remote = command[1] == "--machine"
        verb = tuple(command[3:5] if remote else command[1:3])
        if verb not in PANE_COMMANDS:
            return super().__call__(command)
        self.commands.append(list(command))
        server = self._machine_server(command[2]) if remote else self.local
        rest = command[5:] if remote else command[3:]
        if server is None:
            return subprocess.CompletedProcess(command, 1, "", "unknown machine")
        if remote and command[2] in self.lost and verb != ("agent", "get"):
            raise subprocess.TimeoutExpired(command, 30)
        pane = server["panes"].get(rest[0])
        if pane is None:
            return subprocess.CompletedProcess(command, 1, "", "pane not found")
        if verb == ("agent", "get"):
            reply = {"id": "cli:agent:get", "result": {"agent": pane, "type": "agent"}}
            return subprocess.CompletedProcess(command, 0, json.dumps(reply), "")
        if verb != ("pane", "read"):
            server["received"].append(rest[1])
        return subprocess.CompletedProcess(command, 0, "", "")

    def mutations(self) -> list[list[str]]:
        mutating = {("agent", "prompt"), ("pane", "send-text")}
        found = []
        for command in self.commands:
            remote = command[1] == "--machine"
            if tuple(command[3:5] if remote else command[1:3]) in mutating:
                found.append(command)
        return found

    def received(self) -> dict:
        return {"local": list(self.local["received"])} | {
            execution_id: list(server["received"]) for execution_id, server in sorted(self.remote.items())
        }


class Client:
    """One Anton controller process: a restart builds a new one over the same ownership record and delivery journal."""

    def __init__(self, fleet: Fleet, root: Path) -> None:
        self.fleet, self.root = fleet, root
        self.registrations = Registrations(Catalog(fleet), Store(root / "registrations.json"))
        self.qualifier = Qualifier(machine_probe(fleet))
        self.router = Router(self.registrations.store, Catalog(fleet), self.qualifier, fleet, root / DELIVERIES)

    def register(self, desired: list[Endpoint]) -> dict:
        for endpoint in desired:
            self.fleet.hosts[endpoint.target] = endpoint.scope.execution_id
        return self.registrations.reconcile(desired, self.qualifier).registered


def _refused(action) -> dict:
    try:
        action()
    except EndpointRefused as refused:
        return {"reason": refused.reason, "message": str(refused)}
    return {}


def _target(client: Client, endpoint: Endpoint) -> Target:
    fx = fixture()
    conversation = fx["remote"][endpoint.scope.execution_id]["conversation_id"]
    return client.router.resolve(endpoint.name, fx["pane_id"], conversation)


def _positive_once() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        fleet, desired = Fleet(), sv2_hdr03_cases.endpoints()
        client = Client(fleet, Path(tmp))
        registered = client.register(desired)
        targets = [_target(client, endpoint) for endpoint in desired]
        for endpoint, target in zip(desired, targets, strict=True):
            client.router.send(target, "agent_prompt", f"op-{endpoint.scope.execution_id}", f"for {endpoint.name}")
        return {
            "registered": registered,
            "targets": [target.__dict__ for target in targets],
            "received": fleet.received(),
            "mutations": fleet.mutations(),
            "environment": sorted(remote_environ(fixture()["caller_environment"])),
            "herdr_target_mismatch_total": client.router.herdr_target_mismatch_total(),
        }


def _positive() -> tuple[dict, bool]:
    first, second = _positive_once(), _positive_once()
    desired = sv2_hdr03_cases.endpoints()
    fx = fixture()
    exe_a, exe_b = (endpoint.scope.execution_id for endpoint in desired)
    checks = {
        "each_server_receives_only_its_message": first["received"]
        == {"local": [], exe_a: [f"for {desired[0].name}"], exe_b: [f"for {desired[1].name}"]},
        "both_targets_share_the_pane_id": {target["pane_id"] for target in first["targets"]} == {fx["pane_id"]},
        "targets_carry_profile_session_and_incarnation": [
            (target["profile_id"], target["session"], target["incarnation"]) for target in first["targets"]
        ]
        == [
            (first["registered"][endpoint.scope.execution_id], "default", endpoint.incarnation) for endpoint in desired
        ],
        "every_mutation_names_its_machine": [command[:3] for command in first["mutations"]]
        == [["herdr", "--machine", first["registered"][exe_a]], ["herdr", "--machine", first["registered"][exe_b]]],
        "remote_environment_drops_the_caller_pane_and_socket": first["environment"] == ["PATH"],
        "no_target_mismatch": first["herdr_target_mismatch_total"] == 0,
        "independent_repeat_identical": first == second,
    }
    return {"checks": checks, "first": first, "second_identical": first == second}, all(checks.values())


def _rejections(client: Client, fleet: Fleet, desired: list[Endpoint]) -> dict:
    fx = fixture()
    good = _target(client, desired[0])
    attempts = {
        "bare_remote_pane": lambda: client.router.send(fx["pane_id"], "agent_prompt", "op-bare", "x"),
        "pane_without_machine": lambda: client.router.send(
            Target("", "", "", fx["pane_id"]), "agent_prompt", "op-nomachine", "x"
        ),
        "malformed_pane": lambda: client.router.send(
            Target(good.profile_id, good.session, good.incarnation, "-w1:p1"), "agent_prompt", "op-malformed", "x"
        ),
        "manual_machine_label": lambda: client.router.resolve(
            sv2_hdr03_cases.fixture()["manual"]["label"], fx["pane_id"]
        ),
        "wrong_conversation": lambda: client.router.send(
            Target(good.profile_id, good.session, good.incarnation, fx["pane_id"], fx["local"]["conversation_id"]),
            "agent_prompt",
            "op-conversation",
            "x",
        ),
    }
    observed = {}
    for name, attempt in attempts.items():
        before = (fleet.received(), len(fleet.mutations()))
        refusal = _refused(attempt)
        observed[name] = refusal | {"unchanged": (fleet.received(), len(fleet.mutations())) == before}
    client.register(sv2_hdr03_cases.endpoints(desired[0].scope.generation + 1))
    before = (fleet.received(), len(fleet.mutations()))
    stale = _refused(lambda: client.router.send(good, "agent_prompt", "op-stale", "x"))
    observed["stale_incarnation"] = stale | {"unchanged": (fleet.received(), len(fleet.mutations())) == before}
    return observed


def _rejection_once() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        fleet, desired = Fleet(), sv2_hdr03_cases.endpoints()
        client = Client(fleet, Path(tmp))
        client.register(desired)
        observed = _rejections(client, fleet, desired)
        corrected = _target(client, sv2_hdr03_cases.endpoints(desired[0].scope.generation + 1)[0])
        client.router.send(corrected, "agent_prompt", "op-corrected", "corrected")
        return {
            "refusals": observed,
            "mismatches": sorted([reason, count] for (_, reason), count in client.router.mismatches.items()),
            "herdr_target_mismatch_total": client.router.herdr_target_mismatch_total(),
            "corrected_received": fleet.received(),
        }


def _rejection() -> tuple[dict, bool]:
    first, second = _rejection_once(), _rejection_once()
    refusals = first["refusals"]
    manual = sv2_hdr03_cases.fixture()["manual"]
    exe_a = sv2_hdr03_cases.endpoints()[0].scope.execution_id
    checks = {
        "refused_by_reason": {name: entry.get("reason") for name, entry in refusals.items()}
        == {
            "bare_remote_pane": "bare_pane",
            "pane_without_machine": "bare_pane",
            "malformed_pane": "pane_id",
            "manual_machine_label": "unregistered",
            "wrong_conversation": "conversation_mismatch",
            "stale_incarnation": "stale_incarnation",
        },
        "no_pane_mutated_before_refusal": all(entry["unchanged"] for entry in refusals.values()),
        "every_refusal_counted": first["herdr_target_mismatch_total"] == len(refusals),
        "no_disclosure_of_the_manual_machine": not any(
            manual["id"] in entry["message"] or manual["target"] in entry["message"] for entry in refusals.values()
        ),
        "a_corrected_input_needs_a_new_request": first["corrected_received"][exe_a] == ["corrected"]
        and first["corrected_received"]["local"] == [],
        "independent_repeat_identical": first == second,
    }
    return {"checks": checks, "first": first, "second_identical": first == second}, all(checks.values())


def _recovery_once() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root, fleet, desired = Path(tmp), Fleet(), sv2_hdr03_cases.endpoints()
        client = Client(fleet, root)
        accepted = client.register(desired)
        target = _target(client, desired[0])
        client.router.send(target, "agent_prompt", "op-1", "first prompt")
        fleet.machines = [machine for machine in fleet.machines if machine["id"] != target.profile_id]
        restarted = Client(fleet, root)
        restored = restarted.register(desired)
        old = _refused(lambda: restarted.router.send(target, "agent_prompt", "op-2", "x"))
        refreshed = restarted.router.refresh(target)
        before = len(fleet.mutations())
        replayed = restarted.router.send(refreshed, "agent_prompt", "op-1", "first prompt")
        replay_commands = len(fleet.mutations()) - before
        fleet.lost.add(refreshed.profile_id)
        interrupted = _refused(lambda: restarted.router.send(refreshed, "agent_prompt", "op-3", "lost prompt"))
        fleet.lost.clear()
        again = Client(fleet, root)
        retried = _refused(lambda: again.router.send(refreshed, "agent_prompt", "op-3", "lost prompt"))
        again.register(sv2_hdr03_cases.endpoints(desired[0].scope.generation + 1))
        stale_refresh = _refused(lambda: again.router.refresh(refreshed))
        journal = json.loads((root / DELIVERIES).read_text())
        return {
            "accepted": accepted,
            "restored": restored,
            "old_target": old,
            "refreshed": refreshed.__dict__,
            "replayed": replayed,
            "replay_commands": replay_commands,
            "interrupted": interrupted,
            "retried_after_restart": retried,
            "stale_refresh": stale_refresh,
            "journal": journal,
            "received": fleet.received(),
            "prompt_attempts": [command[-1] for command in fleet.mutations()],
        }


def _recovery() -> tuple[dict, bool]:
    first, second = _recovery_once(), _recovery_once()
    exe_a = sv2_hdr03_cases.endpoints()[0].scope.execution_id
    durable = {key: entry["state"] for key, entry in first["journal"]["deliveries"].items()}
    checks = {
        "reconnect_reregistered_a_new_profile": first["restored"][exe_a] != first["accepted"][exe_a],
        "old_profile_refused_before_any_send": first["old_target"].get("reason") == "unregistered",
        "refresh_keeps_pane_incarnation_and_conversation": first["refreshed"]["profile_id"] == first["restored"][exe_a]
        and first["refreshed"]["pane_id"] == fixture()["pane_id"]
        and first["refreshed"]["incarnation"] == sv2_hdr03_cases.endpoints()[0].incarnation,
        "replay_sends_nothing": first["replayed"] is None and first["replay_commands"] == 0,
        "interrupted_send_is_ambiguous": first["interrupted"].get("reason") == "ambiguous_delivery",
        "restart_never_resends_an_ambiguous_prompt": first["retried_after_restart"].get("reason")
        == "ambiguous_delivery",
        "newer_generation_makes_the_old_target_stale": first["stale_refresh"].get("reason") == "stale_incarnation",
        "durable_journal_records_what_survived": durable == {"op-1": "sent", "op-3": "pending"},
        "each_prompt_delivered_once": first["received"][exe_a] == ["first prompt"]
        and first["prompt_attempts"].count("lost prompt") == 1,
        "independent_repeat_identical": first == second,
    }
    return {"checks": checks, "first": first, "second_identical": first == second}, all(checks.values())


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-HDR-04-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": copy.deepcopy(observed),
    }
