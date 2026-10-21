import copy
import hashlib
import json
import socket
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.herdr.attach import SSH, ClientHome, Terminal
from scripts.swarm_v2.herdr.capabilities import Qualifier, machine_probe, machine_status
from scripts.swarm_v2.herdr.catalog import Catalog
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused, TerminalAuthority, operator_scope
from scripts.swarm_v2.herdr.reconcile import Record, Registrations, Report, Store
from scripts.swarm_v2.operator_auth import OPERATOR
from tests import sv2_hdr01_cases

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("herdr-registrations.json", "herdr-capabilities.json")
NOT_READY = "error: remote server is not ready for saved machines; machine was not saved"
EVIDENCE_CLASS = (
    "mocked: a recording stand in for the Anton herdr client answers herdr machine list, add, rename and remove "
    "with the 0.9.1 reply shapes captured in the SV2-HDR-01 live proof, and its status and schema replies are the "
    "SV2-HDR-01 captures; no herdr process, SSH connection or cluster runs in these cases"
)


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-registrations.json").read_text())


def endpoints(*generations: int) -> list[Endpoint]:
    fx = fixture()
    managed = [dict(entry) for entry in fx["managed"]]
    for entry, generation in zip(managed, generations, strict=False):
        entry["generation"] = generation
    return [
        Endpoint(operator_scope(OPERATOR, fx["swarm"], SimpleNamespace(**entry)), fx["namespace"]) for entry in managed
    ]


def _flag(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


class Herdr:
    """Stands in for the Anton herdr client's saved machine catalog and the worker servers behind it."""

    def __init__(self, machines: list[dict], ready: set[str]) -> None:
        self.machines = copy.deepcopy(machines)
        self.ready = set(ready)
        self.servers: dict[str, dict] = {}
        self.unanswered: set[str] = set()
        self.listing: str | None = None
        self.saves = 0
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(list(command))
        if command[:2] == ["herdr", "machine"]:
            return getattr(self, f"_{command[2]}")(command)
        label = command[2] if command[1] == "--machine" else None
        server = self.servers.get(label, sv2_hdr01_cases.fixture()["server"])
        forwarded = [*command[:2], sv2_hdr01_cases.TARGET, *command[3:]] if label else command
        return sv2_hdr01_cases.Runner(server)(forwarded)

    def _list(self, command: list[str]) -> subprocess.CompletedProcess:
        listing = json.dumps(self.machines, indent=2) if self.listing is None else self.listing
        return subprocess.CompletedProcess(command, 0, listing, "")

    def _add(self, command: list[str]) -> subprocess.CompletedProcess:
        target = command[-1]
        if target not in self.ready:
            return subprocess.CompletedProcess(command, 1, "", NOT_READY)
        self.saves += 1
        label = _flag(command, "--label")
        profile = hashlib.sha256(f"{label}/{self.saves}".encode()).hexdigest()[:32]
        session = _flag(command, "--remote-session")
        saved = {"id": profile, "label": label, "target": target, "session": session, "enabled": True}
        self.machines.append(saved | {"selected": False})
        if target in self.unanswered:
            raise subprocess.TimeoutExpired(command, 30)
        reply = f"Saved SSH machine {profile}. Remote server is ready.\nOpen Herdr clients connect automatically.\n"
        return subprocess.CompletedProcess(command, 0, reply, "")

    def _rename(self, command: list[str]) -> subprocess.CompletedProcess:
        if len(command) != 6 or command[4] != "--label":
            return subprocess.CompletedProcess(
                command, 2, "", "usage: herdr machine rename <profile-id> --label <label>\n"
            )
        for machine in self.machines:
            if machine["id"] == command[3]:
                machine["label"] = command[5]
                return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 1, "", "unknown machine")

    def _remove(self, command: list[str]) -> subprocess.CompletedProcess:
        kept = [machine for machine in self.machines if machine["id"] != command[-1]]
        found, self.machines = len(kept) != len(self.machines), kept
        return subprocess.CompletedProcess(command, 0 if found else 1, "", "" if found else "unknown machine")

    def adds(self, label: str) -> int:
        return sum(1 for command in self.commands if command[:3] == ["herdr", "machine", "add"] and label in command)

    def mutating(self) -> list[list[str]]:
        return [command for command in self.commands if command[:3] in MUTATING]


MUTATING = (["herdr", "machine", "add"], ["herdr", "machine", "rename"], ["herdr", "machine", "remove"])


class Client:
    """One Anton herdr client process: a restart builds a new one over the same store and catalog."""

    def __init__(self, herdr: Herdr, store: Path) -> None:
        self.herdr = herdr
        self.registrations = Registrations(Catalog(herdr), Store(store))
        self.qualifier = Qualifier(machine_probe(herdr), machine_status(herdr))

    def reconcile(self, desired: list[Endpoint]) -> Report:
        return self.registrations.reconcile(desired, self.qualifier)

    def records(self) -> dict:
        records = self.registrations.store.load().items()
        return {key: asdict(record) | {"before": list(record.before)} for key, record in records}


def _report(report: Report) -> dict:
    return asdict(report) | {"herdr_registration_drift": report.herdr_registration_drift()}


def _refused(action) -> dict:
    try:
        action()
    except EndpointRefused as refused:
        return {"reason": refused.reason, "message": str(refused)}
    return {}


def _sidebar(herdr: Herdr) -> list[str]:
    return ["Local", *(machine["label"] for machine in herdr.machines if machine["enabled"])]


def _positive_once() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        desired, manual = endpoints(), fixture()["manual"]
        herdr = Herdr([manual], {endpoint.target for endpoint in desired})
        client = Client(herdr, Path(tmp) / "registrations.json")
        first, second = client.reconcile(desired), client.reconcile(desired)
        by_id = {machine["id"]: machine for machine in herdr.machines}
        return {
            "first": _report(first),
            "replayed": _report(second),
            "registrations": {
                execution_id: {name: by_id[profile_id][name] for name in ("id", "label", "target", "session")}
                for execution_id, profile_id in first.registered.items()
            },
            "records": client.records(),
            "verified_incarnations": {
                label: verdict.incarnation for label, verdict in sorted(client.qualifier.verdicts.items())
            },
            "sidebar": _sidebar(herdr),
            "manual_untouched": manual in herdr.machines,
            "commands_naming_manual": [command for command in herdr.commands if manual["id"] in command],
            "mutating": herdr.mutating(),
        }


def _positive() -> tuple[dict, bool]:
    first, second = _positive_once(), _positive_once()
    desired = endpoints()
    registrations = first["registrations"]
    checks = {
        "each_registration_maps_to_its_own_execution": all(
            registrations[endpoint.scope.execution_id]
            == {
                "id": first["first"]["registered"][endpoint.scope.execution_id],
                "label": endpoint.name,
                "target": endpoint.target,
                "session": "default",
            }
            for endpoint in desired
        )
        and len({entry["id"] for entry in registrations.values()}) == 2,
        "verdicts_keyed_by_endpoint_incarnation": first["verified_incarnations"]
        == {endpoint.name: endpoint.incarnation for endpoint in desired},
        "two_workers_beside_local": first["sidebar"] == ["Local", "devbox", desired[0].name, desired[1].name],
        "store_matches_catalog": {key: record["profile_id"] for key, record in first["records"].items()}
        == first["first"]["registered"],
        "nothing_refused": first["first"]["refused"] == {} and first["first"]["retired"] == [],
        "manual_machine_untouched": first["manual_untouched"] and not first["commands_naming_manual"],
        "replay_adds_nothing": first["replayed"]["registered"] == first["first"]["registered"]
        and first["replayed"]["herdr_registration_drift"] == 0
        and len(first["mutating"]) == 2,
        "independent_repeat_identical": first == second,
    }
    return {"checks": checks, "first": first, "second_identical": first == second}, all(checks.values())


def _records(client: Client) -> dict | str:
    try:
        return client.records()
    except EndpointRefused as refused:
        return refused.reason


def _scenario(change) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        desired, manual = endpoints(), copy.deepcopy(fixture()["manual"])
        store = Path(tmp) / "registrations.json"
        herdr = Herdr([manual], {endpoint.target for endpoint in desired})
        desired = change(herdr, desired, store)
        protected = [machine for machine in herdr.machines if machine["id"] == manual["id"]]
        stored = store.read_text() if store.exists() else None
        client = Client(herdr, store)
        reports: list[Report] = []
        refusal = _refused(lambda: reports.append(client.reconcile(desired)))
        return {
            "refusal": refusal,
            "report": _report(reports[0]) if reports else None,
            "manual_before": protected,
            "manual_after": [machine for machine in herdr.machines if machine["id"] == manual["id"]],
            "commands_naming_manual": [command for command in herdr.commands if manual["id"] in command],
            "mutating": herdr.mutating(),
            "store_unchanged": (store.read_text() if store.exists() else None) == stored,
            "records": _records(client),
        }


def _label_taken(herdr: Herdr, desired: list[Endpoint], store: Path) -> list[Endpoint]:
    herdr.machines[0]["label"] = desired[0].name
    return desired


def _target_taken(herdr: Herdr, desired: list[Endpoint], store: Path) -> list[Endpoint]:
    herdr.machines[0]["target"] = desired[0].target
    return desired


def _incompatible(herdr: Herdr, desired: list[Endpoint], store: Path) -> list[Endpoint]:
    herdr.servers[desired[1].name] = sv2_hdr01_cases.fixture()["missing_forwarding_server"]
    return desired


def _not_ready(herdr: Herdr, desired: list[Endpoint], store: Path) -> list[Endpoint]:
    herdr.ready.discard(desired[1].target)
    return desired


def _unreadable_catalog(herdr: Herdr, desired: list[Endpoint], store: Path) -> list[Endpoint]:
    herdr.listing = "{"
    return desired


def _unreadable_ownership(herdr: Herdr, desired: list[Endpoint], store: Path) -> list[Endpoint]:
    store.write_text("{")
    return desired


def _retired_beside_manual(herdr: Herdr, desired: list[Endpoint], store: Path) -> list[Endpoint]:
    herdr.machines[0]["label"] = desired[0].name
    endpoint = desired[0]
    scope = endpoint.scope
    gone = Record(scope.execution_id, scope.generation, endpoint.name, endpoint.target, "default", "gone")
    Store(store).save({scope.execution_id: gone})
    return []


def _direct_collision() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        desired, manual = endpoints(), copy.deepcopy(fixture()["manual"])
        manual["label"] = desired[0].name
        herdr = Herdr([manual], {desired[0].target})
        client = Client(herdr, Path(tmp) / "registrations.json")
        refusal = _refused(lambda: client.registrations.register(desired[0]))
        return refusal | {
            "discloses_manual_target": manual["target"] in refusal["message"],
            "mutating": herdr.mutating(),
            "store_written": (Path(tmp) / "registrations.json").exists(),
        }


SCENARIOS = {
    "label_collision": _label_taken,
    "target_collision": _target_taken,
    "incompatible_server": _incompatible,
    "server_not_ready": _not_ready,
    "unreadable_catalog": _unreadable_catalog,
    "unreadable_ownership": _unreadable_ownership,
    "retired_record_beside_manual_label": _retired_beside_manual,
}


def _isolation() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        desired, manual = endpoints(), copy.deepcopy(fixture()["manual"])
        manual["label"] = desired[0].name
        herdr = Herdr([manual], {endpoint.target for endpoint in desired})
        client = Client(herdr, Path(tmp) / "registrations.json")
        refused = client.reconcile(desired)
        herdr.machines[0]["label"] = "devbox"
        retried = client.reconcile(desired)
        return {
            "refused": _report(refused),
            "after_the_operator_renamed_the_manual_machine": _report(retried),
            "manual_after": herdr.machines[0],
        }


def _discloses(entry: dict) -> bool:
    manual = fixture()["manual"]
    shown = json.dumps({key: entry.get(key) for key in ("refusal", "report", "message")})
    return manual["id"] in shown or manual["target"] in shown


def _rejection() -> tuple[dict, bool]:
    observed = {name: _scenario(change) for name, change in SCENARIOS.items()}
    observed |= {"direct": _direct_collision(), "recovery_isolation": _isolation()}
    exe_a, exe_b = (endpoint.scope.execution_id for endpoint in endpoints())
    scenarios = [observed[name] for name in SCENARIOS]
    refused = {
        name: (observed[name]["report"] or {}).get("refused")
        for name in ("label_collision", "target_collision", "incompatible_server", "server_not_ready")
    }
    isolation = observed["recovery_isolation"]
    checks = {
        "conflicts_refused_by_reason": refused
        == {
            "label_collision": {exe_a: "label_collision"},
            "target_collision": {exe_a: "target_collision"},
            "incompatible_server": {exe_b: "incompatible"},
            "server_not_ready": {exe_b: "machine_refused"},
        },
        "manual_machine_never_mutated": all(
            entry["manual_before"] == entry["manual_after"] and not entry["commands_naming_manual"]
            for entry in scenarios
        ),
        "refused_endpoints_hold_no_ownership": all(
            sorted(observed[name]["records"]) == [exe_b] for name in ("label_collision", "target_collision")
        )
        and all(sorted(observed[name]["records"]) == [exe_a] for name in ("incompatible_server", "server_not_ready")),
        "incompatible_registration_removed": observed["incompatible_server"]["mutating"][-1][:3]
        == ["herdr", "machine", "remove"],
        "unreadable_state_mutates_nothing": observed["unreadable_catalog"]["refusal"]["reason"] == "catalog_unreadable"
        and observed["unreadable_ownership"]["refusal"]["reason"] == "ownership_unreadable"
        and not observed["unreadable_catalog"]["mutating"]
        and not observed["unreadable_ownership"]["mutating"]
        and observed["unreadable_ownership"]["store_unchanged"],
        "retired_record_spares_a_same_label_manual_machine": observed["retired_record_beside_manual_label"]["report"][
            "retired"
        ]
        == [exe_a]
        and not observed["retired_record_beside_manual_label"]["mutating"],
        "direct_collision_writes_nothing": observed["direct"]["reason"] == "label_collision"
        and not observed["direct"]["mutating"]
        and not observed["direct"]["store_written"],
        "no_disclosure_of_the_manual_machine": not observed["direct"]["discloses_manual_target"]
        and not any(_discloses(entry) for entry in scenarios),
        "a_corrected_input_needs_a_new_request": isolation["refused"]["refused"] == {exe_a: "label_collision"}
        and exe_a in isolation["after_the_operator_renamed_the_manual_machine"]["registered"]
        and isolation["manual_after"]["label"] == "devbox",
    }
    return {"checks": checks} | observed, all(checks.values())


def _restart_restores(root: Path) -> dict:
    desired, manual = endpoints(), fixture()["manual"]
    herdr = Herdr([manual], {endpoint.target for endpoint in desired})
    store = root / "restart.json"
    accepted = Client(herdr, store).reconcile(desired)
    kept = accepted.registered[desired[0].scope.execution_id]
    herdr.machines = [machine for machine in herdr.machines if machine["label"] != desired[1].name]
    restored = Client(herdr, store).reconcile(desired)
    lost = accepted.registered[desired[1].scope.execution_id]
    durability = {
        "survived": kept,
        "lost": lost,
        "restored_as": restored.registered[desired[1].scope.execution_id],
        "lost_still_saved": any(machine["id"] == lost for machine in herdr.machines),
    }
    replayed = Client(herdr, store).reconcile(desired)
    herdr.machines[1]["label"] = "renamed-by-hand"
    relabelled = Client(herdr, store).reconcile(desired)
    bumped = Client(herdr, store).reconcile(endpoints(3))
    stale = Client(herdr, store).reconcile(endpoints(2))
    after_stale = Client(herdr, store)
    generation_after_stale = after_stale.records()[desired[0].scope.execution_id]["generation"]
    retired = after_stale.reconcile(desired[1:])
    return {
        "accepted": _report(accepted),
        "restored": _report(restored),
        "replayed": _report(replayed),
        "relabelled": _report(relabelled),
        "bumped": _report(bumped),
        "stale": _report(stale),
        "generation_after_stale": generation_after_stale,
        "retired": _report(retired),
        "kept_profile_survived": restored.registered[desired[0].scope.execution_id] == kept,
        "durability": durability,
        "adds": {endpoint.name: herdr.adds(endpoint.name) for endpoint in desired},
        "renames": [command for command in herdr.commands if command[:3] == ["herdr", "machine", "rename"]],
        "records": after_stale.records(),
        "sidebar": _sidebar(herdr),
    }


def _interrupted_add(root: Path) -> dict:
    desired, manual = endpoints(), fixture()["manual"]
    herdr = Herdr([manual], {endpoint.target for endpoint in desired})
    herdr.unanswered.add(desired[0].target)
    store = root / "interrupted.json"
    interrupted = Client(herdr, store)
    first = interrupted.reconcile(desired)
    pending = interrupted.records()[desired[0].scope.execution_id]["profile_id"]
    herdr.unanswered.clear()
    replay = Client(herdr, store)
    second = replay.reconcile(desired)
    return {
        "first": _report(first),
        "pending_profile_id": pending,
        "replayed": _report(second),
        "adds": herdr.adds(desired[0].name),
        "saved": [machine["id"] for machine in herdr.machines if machine["label"] == desired[0].name],
        "records": replay.records(),
    }


def _recovery() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as tmp:
        restart, interrupted = _restart_restores(Path(tmp)), _interrupted_add(Path(tmp))
    desired = endpoints()
    exe_a, exe_b = (endpoint.scope.execution_id for endpoint in desired)
    checks = {
        "restart_restores_the_missing_registration": restart["restored"]["drift"] == {"missing": 1}
        and restart["kept_profile_survived"],
        "restart_duplicates_nothing": restart["adds"] == {desired[0].name: 1, desired[1].name: 2},
        "durability_survivors_and_losses_recorded": restart["durability"]["survived"]
        == restart["restored"]["registered"][exe_a]
        and restart["durability"]["lost"] != restart["durability"]["restored_as"]
        and not restart["durability"]["lost_still_saved"],
        "replay_has_no_drift": restart["replayed"]["herdr_registration_drift"] == 0,
        "label_drift_renamed_back": restart["relabelled"]["drift"] == {"label": 1} and len(restart["renames"]) == 1,
        "new_generation_keeps_its_profile": restart["bumped"]["registered"] == restart["replayed"]["registered"],
        "stale_generation_cannot_overwrite": restart["stale"]["refused"] == {exe_a: "stale_generation"}
        and restart["generation_after_stale"] == 3,
        "retired_registration_removed_only": restart["retired"]["retired"] == [exe_a]
        and sorted(restart["records"]) == [exe_b]
        and restart["sidebar"] == ["Local", "devbox", desired[1].name],
        "interrupted_add_left_pending": interrupted["first"]["refused"] == {exe_a: "catalog_unreachable"}
        and interrupted["pending_profile_id"] is None,
        "interrupted_add_adopted_not_duplicated": interrupted["replayed"]["drift"] == {"interrupted": 1}
        and interrupted["adds"] == 1
        and interrupted["replayed"]["registered"][exe_a] == interrupted["saved"][0]
        and interrupted["records"][exe_a]["profile_id"] == interrupted["saved"][0],
    }
    observed = {"checks": checks, "restart": restart, "interrupted_add": interrupted}
    return observed, all(checks.values())


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-HDR-03-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }


HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey"


def client_terminal(root: Path, herdr: Herdr, namespace: str, addresses=("10.43.12.7",)) -> Terminal:
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: 1_800_000_000)

    def lookup(host, port, type):
        return [(socket.AF_INET, type, 6, "", (address, port)) for address in addresses]

    def run(command: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 0, "", "") if command[0] == SSH else herdr(command)

    return Terminal(ClientHome(root), authority, namespace, run, lookup)
