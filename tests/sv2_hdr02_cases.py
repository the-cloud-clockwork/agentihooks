import base64
import hashlib
import json
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.herdr import endpoints
from scripts.swarm_v2.herdr.attach import ClientHome, Terminal
from scripts.swarm_v2.herdr.endpoints import (
    Clients,
    Endpoint,
    EndpointRefused,
    HostIdentity,
    TerminalAuthority,
    grant_scope,
    operator_scope,
)
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.runtime.commands import Principal, Role
from tests import sv2_hdr01_cases

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("herdr-terminal.json", "herdr-terminal-objects.json", "herdr-capabilities.json")
EVIDENCE_CLASS = (
    "mocked: a recording runner stands in for ssh and herdr; it admits a connection only when the client's pinned "
    "known_hosts key matches the key the fixture server serves and the client certificate, verified against the "
    "fixture user CA, names the server's execution principal; herdr status and schema replies are the SV2-HDR-01 "
    "captures of the pinned 0.9.1 binary. No sshd, herdr process or cluster runs in these cases."
)


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-terminal.json").read_text())


def _openssh(key) -> str:
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


class World:
    """Stands in for the cluster network, each worker sshd and the client's herdr."""

    def __init__(self, home: ClientHome, user_ca: str) -> None:
        self.home, self.user_ca = home, user_ca
        self.servers: dict[str, dict] = {}
        self.dns: dict[str, list[str]] = {}
        self.machines: list[dict] = []
        self.commands: list[list[str]] = []
        self.herdr = sv2_hdr01_cases.Runner(sv2_hdr01_cases.fixture()["server"])

    def serve(self, endpoint: Endpoint, identity: HostIdentity, address: str = "10.43.12.7") -> None:
        self.servers[endpoint.host] = {"key": identity.public, "principal": endpoint.scope.execution_id}
        self.dns[endpoint.host] = [address]

    def lookup(self, host, port, type):  # noqa: A002
        if host not in self.dns:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, type, 6, "", (address, port)) for address in self.dns[host]]

    def _ssh(self, host: str) -> subprocess.CompletedProcess:
        server = self.servers[host]
        name = host.split(".", 1)[0]
        pinned = (self.home.root / "known_hosts" / name).read_text().split(" ", 1)[1].strip()
        if pinned != server["key"]:
            return subprocess.CompletedProcess([], 255, "", "Host key verification failed.\r\n")
        certificate = serialization.load_ssh_public_identity(
            (self.home.root / "certificates" / f"{name}-cert.pub").read_bytes()
        )
        certificate.verify_cert_signature()
        trusted = _openssh(certificate.signature_key()) == self.user_ca
        if not trusted or certificate.valid_principals != [server["principal"].encode()]:
            return subprocess.CompletedProcess([], 255, "", "Permission denied (publickey).\r\n")
        return subprocess.CompletedProcess([], 0, "", "")

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append([part.replace(str(self.home.root), "<home>") for part in command])
        if command[0] == str(self.home.ssh):
            return self._ssh(command[1])
        if command[:3] == ["herdr", "machine", "list"]:
            return subprocess.CompletedProcess([], 0, json.dumps(self.machines), "")
        if command[:3] == ["herdr", "machine", "add"]:
            self.machines.append({"label": command[4], "target": command[5]})
            return subprocess.CompletedProcess([], 0, "", "")
        forwarded = [*command[:2], sv2_hdr01_cases.TARGET, *command[3:]] if "--machine" in command else command
        return self.herdr(forwarded)


def _registration(execution_id: str, generation: int) -> Registration:
    record = fixture()["registration"] | {"execution_id": execution_id, "generation": generation}
    return Registration(**record)


def _setup(root: Path, authority: TerminalAuthority | None = None, user_ca: str | None = None):
    authority = authority or TerminalAuthority(Ed25519PrivateKey.generate(), lambda: fixture()["now"])
    home = ClientHome(root / "client")
    world = World(home, user_ca or authority.user_ca)
    return Terminal(home, authority, fixture()["namespace"], world, world.lookup), world


def _refused(action) -> str:
    try:
        action()
    except EndpointRefused as refused:
        return refused.reason
    return ""


def _listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")) if root.exists() else []


def _rendered(endpoint: Endpoint) -> dict:
    fx = fixture()
    identity = HostIdentity(b"fixture host key\n", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey")
    user_ca = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureUserCa"
    items = endpoints.objects(endpoint, fx["pod"], identity, user_ca, Clients(**fx["clients"]))
    principals = base64.b64decode(items[0]["data"][endpoints.PRINCIPALS]).decode()
    return {
        "kinds": [item["kind"] for item in items],
        "names": sorted({item["metadata"]["name"] for item in items}),
        "service_type": items[1]["spec"]["type"],
        "service_selector": items[1]["spec"]["selector"],
        "policy_client_namespaces": [
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for peer in items[2]["spec"]["ingress"][0]["from"]
        ],
        "exposure_findings": [endpoints.public_exposure(endpoint, item) for item in items],
        "secret_principals": principals,
        "matches_golden": items == json.loads((FIXTURES / "herdr-terminal-objects.json").read_text())["items"],
    }


def _positive_once() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        terminal, world = _setup(Path(tmp))
        scope = grant_scope(_registration(fixture()["registration"]["execution_id"], 2))
        endpoint = Endpoint(scope, fixture()["namespace"])
        world.serve(endpoint, HostIdentity.generate())
        verdict = terminal.attach(scope, world.servers[endpoint.host]["key"])
        other = Endpoint(
            grant_scope(_registration(fixture()["other_execution"]["execution_id"], 1)), endpoint.namespace
        )
        world.serve(other, HostIdentity.generate(), "10.43.12.9")
        certificate = serialization.load_ssh_public_identity(terminal.home.certificate(endpoint).read_bytes())
        return {
            "rendered": _rendered(endpoint),
            "commands": world.commands,
            "verdict": {"target": verdict.target, "incarnation": verdict.incarnation, "compatible": verdict.compatible},
            "certificate": {
                "key_id": certificate.key_id.decode(),
                "principals": [p.decode() for p in certificate.valid_principals],
            },
            "machines": world.machines,
            "other_endpoint_admits_this_certificate": [p.decode() for p in certificate.valid_principals]
            == [world.servers[other.host]["principal"]],
            "client_files": _listing(terminal.home.root),
            "terminal_endpoint_auth_failures": terminal.terminal_endpoint_auth_failures(),
        }


def _positive() -> tuple[dict, bool]:
    first, second = _positive_once(), _positive_once()
    passed = (
        first == second
        and first["verdict"]["compatible"]
        and first["rendered"]["matches_golden"]
        and first["rendered"]["service_type"] == "ClusterIP"
        and first["rendered"]["exposure_findings"] == [[], [], []]
        and not first["other_endpoint_admits_this_certificate"]
        and first["terminal_endpoint_auth_failures"] == {}
    )
    return {"first": first, "second_identical": first == second}, passed


def _rejection_case(change) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        terminal, world = _setup(Path(tmp))
        scope = grant_scope(_registration(fixture()["registration"]["execution_id"], 2))
        endpoint = Endpoint(scope, fixture()["namespace"])
        identity = HostIdentity.generate()
        world.serve(endpoint, identity)
        pinned = change(world, endpoint, identity)
        before = _listing(terminal.home.root)
        reason = _refused(lambda: terminal.attach(scope, pinned))
        return {
            "reason": reason,
            "commands": world.commands,
            "machines": world.machines,
            "client_files_before": before,
            "client_files_after": _listing(terminal.home.root),
            "terminal_endpoint_auth_failures": terminal.terminal_endpoint_auth_failures(),
        }


def _unauthorized_client(world: World, endpoint: Endpoint, identity: HostIdentity) -> str:
    world.user_ca = _openssh(Ed25519PrivateKey.generate().public_key())
    return identity.public


def _other_principal(world: World, endpoint: Endpoint, identity: HostIdentity) -> str:
    world.servers[endpoint.host]["principal"] = fixture()["other_execution"]["execution_id"]
    return identity.public


def _changed_host_key(world: World, endpoint: Endpoint, identity: HostIdentity) -> str:
    world.servers[endpoint.host]["key"] = HostIdentity.generate().public
    return identity.public


def _missing_dns(world: World, endpoint: Endpoint, identity: HostIdentity) -> str:
    del world.dns[endpoint.host]
    return identity.public


def _public_route(world: World, endpoint: Endpoint, identity: HostIdentity) -> str:
    world.dns[endpoint.host] = fixture()["routes"]["public"]
    return identity.public


def _planted_exposure() -> dict:
    endpoint = Endpoint(grant_scope(_registration(fixture()["registration"]["execution_id"], 2)), "swarm-workers")
    service = endpoints.service(endpoint, {})
    service["spec"].update(type="LoadBalancer", externalIPs=["1.1.1.1"])
    service["spec"]["ports"][0]["nodePort"] = 30022
    policy = endpoints.network_policy(endpoint, {}, Clients(**fixture()["clients"]))
    policy["spec"]["ingress"][0]["from"].append({"ipBlock": {"cidr": "0.0.0.0/0"}})
    return {
        "service": endpoints.public_exposure(endpoint, service),
        "policy": endpoints.public_exposure(endpoint, policy),
    }


def _other_role() -> str:
    execution = SimpleNamespace(**fixture()["other_execution"])
    return _refused(lambda: operator_scope(Principal("master@fixture", Role.MASTER), "fixture-swarm", execution))


def _rejection() -> tuple[dict, bool]:
    observed = {
        "unauthorized_client": _rejection_case(_unauthorized_client),
        "certificate_for_another_execution": _rejection_case(_other_principal),
        "changed_host_key": _rejection_case(_changed_host_key),
        "missing_cluster_dns_route": _rejection_case(_missing_dns),
        "public_route": _rejection_case(_public_route),
        "planted_public_exposure": _planted_exposure(),
        "non_operator_role": _other_role(),
    }
    expected = {
        "unauthorized_client": "unauthorized",
        "certificate_for_another_execution": "unauthorized",
        "changed_host_key": "host_key_changed",
        "missing_cluster_dns_route": "no_route",
        "public_route": "public_route",
    }
    refused = all(observed[name]["reason"] == reason for name, reason in expected.items())
    untouched = all(
        not observed[name]["machines"] and observed[name]["terminal_endpoint_auth_failures"] == {reason: 1}
        for name, reason in expected.items()
    )
    unrouted = [observed[name] for name in ("missing_cluster_dns_route", "public_route")]
    passed = (
        refused
        and untouched
        and all(not entry["commands"] and not entry["client_files_after"] for entry in unrouted)
        and len(observed["planted_public_exposure"]["service"]) == 3
        and len(observed["planted_public_exposure"]["policy"]) == 1
        and observed["non_operator_role"] == "unauthorized"
    )
    return observed, passed


def _recovery() -> tuple[dict, bool]:
    with tempfile.TemporaryDirectory() as tmp:
        terminal, world = _setup(Path(tmp))
        first = grant_scope(_registration(fixture()["registration"]["execution_id"], 2))
        old = Endpoint(first, fixture()["namespace"])
        old_identity = HostIdentity.generate()
        world.serve(old, old_identity)
        steps = {"accepted": terminal.attach(first, old_identity.public).incarnation}
        replacement = HostIdentity.generate()
        world.servers[old.host]["key"] = replacement.public
        steps["old_name_with_new_key"] = _refused(lambda: terminal.attach(first, old_identity.public))
        steps["old_pin_kept"] = terminal.home.known_hosts(old).read_text() == f"{old.host} {old_identity.public}\n"
        operator = SimpleNamespace(**fixture()["other_execution"])
        second = operator_scope(OPERATOR, "fixture-swarm", operator)
        new = Endpoint(second, fixture()["namespace"])
        world.serve(new, replacement, "10.43.12.9")
        steps["replacement"] = terminal.attach(second, replacement.public).incarnation
        steps["replayed"] = terminal.attach(second, replacement.public).incarnation
        steps["machines"] = world.machines
        steps["machine_adds"] = sum(1 for command in world.commands if command[:3] == ["herdr", "machine", "add"])
        steps["old_pin_after_replacement"] = (
            terminal.home.known_hosts(old).read_text() == f"{old.host} {old_identity.public}\n"
        )
        steps["terminal_endpoint_auth_failures"] = terminal.terminal_endpoint_auth_failures()
        passed = (
            steps["accepted"] == fixture()["registration"]["execution_id"]
            and steps["old_name_with_new_key"] == "host_key_changed"
            and steps["old_pin_kept"]
            and steps["replacement"] == steps["replayed"] == fixture()["other_execution"]["execution_id"]
            and steps["machine_adds"] == 2
            and steps["old_pin_after_replacement"]
            and steps["terminal_endpoint_auth_failures"] == {"host_key_changed": 1}
        )
        return steps, passed


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-HDR-02-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
