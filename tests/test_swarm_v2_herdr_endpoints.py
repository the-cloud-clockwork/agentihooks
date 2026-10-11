import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.herdr import endpoints
from scripts.swarm_v2.herdr.endpoints import (
    Clients,
    Endpoint,
    EndpointRefused,
    HostIdentity,
    Scope,
    TerminalAuthority,
    grant_scope,
    operator_scope,
)
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.runtime.commands import Principal, Role
from tests import sv2_hdr02_cases as cases

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-HDR-02"
EXE_A = "exe-" + "a" * 32
OWNER = {
    "swarm.agentihooks.io/controller-owner": "agentihooks-swarm-fixture-swarm",
    "swarm.agentihooks.io/execution-id": EXE_A,
    "swarm.agentihooks.io/generation": "2",
}
IDENTITY = HostIdentity(b"fixture host key\n", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey")
USER_CA = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureUserCa"


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-terminal.json").read_text())


def scope() -> Scope:
    return grant_scope(Registration(**fixture()["registration"]))


def endpoint() -> Endpoint:
    return Endpoint(scope(), "swarm-workers")


def clients() -> Clients:
    return Clients(**fixture()["clients"])


def refusal(action) -> tuple[str, str]:
    with pytest.raises(EndpointRefused) as caught:
        action()
    return caught.value.reason, str(caught.value)


def test_a_launch_grant_scopes_its_own_execution():
    assert scope() == Scope("grant/lgr-" + "c" * 32, "fixture-swarm", EXE_A, 2)


def test_the_operator_role_scopes_the_execution_it_selects():
    execution = SimpleNamespace(execution_id=EXE_A, generation=3)
    assert operator_scope(OPERATOR, "fixture-swarm", execution) == Scope("operator/operator", "fixture-swarm", EXE_A, 3)


def test_another_role_gets_no_terminal_scope():
    master = Principal("master@fixture", Role.MASTER)
    execution = SimpleNamespace(execution_id=EXE_A, generation=1)
    assert refusal(lambda: operator_scope(master, "fixture-swarm", execution)) == (
        "unauthorized",
        "only a launch grant or the operator role opens a worker terminal",
    )


@pytest.mark.parametrize(
    ("swarm", "execution_id", "generation", "message"),
    [
        (None, EXE_A, 1, "terminal scope swarm is not a label value"),
        ("bad swarm", EXE_A, 1, "terminal scope swarm is not a label value"),
        ("fixture-swarm", None, 1, "terminal scope execution is not an execution id"),
        ("fixture-swarm", "swarm-workers.svc", 1, "terminal scope execution is not an execution id"),
        ("fixture-swarm", EXE_A + "0", 1, "terminal scope execution is not an execution id"),
        ("fixture-swarm", EXE_A, 0, "terminal scope generation is not a positive integer"),
        ("fixture-swarm", EXE_A, True, "terminal scope generation is not a positive integer"),
        ("fixture-swarm", EXE_A, "1", "terminal scope generation is not a positive integer"),
    ],
)
def test_a_scope_needs_a_swarm_an_execution_and_a_generation(swarm, execution_id, generation, message):
    execution = SimpleNamespace(execution_id=execution_id, generation=generation)
    assert refusal(lambda: operator_scope(OPERATOR, swarm, execution)) == ("scope", message)


def test_the_lowest_generation_is_one():
    execution = SimpleNamespace(execution_id=EXE_A, generation=1)
    assert operator_scope(OPERATOR, "fixture-swarm", execution).generation == 1


def test_the_endpoint_names_come_from_the_execution():
    found = endpoint()
    name = f"swarm-{EXE_A}-terminal"
    assert (found.name, found.host, found.target, found.incarnation, found.labels) == (
        name,
        f"{name}.swarm-workers.svc.cluster.local",
        f"ssh://{name}.swarm-workers.svc.cluster.local",
        EXE_A,
        OWNER,
    )


@pytest.mark.parametrize("namespace", [None, "", "Swarm", "-swarm", "a" * 64])
def test_an_endpoint_needs_a_namespace_name(namespace):
    assert refusal(lambda: Endpoint(scope(), namespace)) == ("namespace", "terminal namespace is not a namespace name")


@pytest.mark.parametrize(
    ("namespace", "labels", "message"),
    [
        (None, {"a": "b"}, "terminal client namespace is not a namespace name"),
        ("Bad", {"a": "b"}, "terminal client namespace is not a namespace name"),
        ("swarm-control", {}, "terminal clients must be selected by at least one label"),
        ("swarm-control", {"a": ""}, "terminal clients must be selected by at least one label"),
        ("swarm-control", {"a": "b", "c": 1}, "terminal clients must be selected by at least one label"),
    ],
)
def test_terminal_clients_are_a_namespace_and_a_label_selector(namespace, labels, message):
    assert refusal(lambda: Clients(namespace, labels)) == ("clients", message)


def test_the_rendered_objects_match_the_golden_manifests():
    rendered = endpoints.objects(endpoint(), fixture()["pod"], IDENTITY, USER_CA, clients())
    golden = json.loads((FIXTURES / "herdr-terminal-objects.json").read_text())
    assert rendered == golden["items"]


def test_the_secret_carries_the_host_key_the_user_ca_and_the_only_principal():
    secret = endpoints.objects(endpoint(), fixture()["pod"], IDENTITY, USER_CA, clients())[0]
    decoded = {name: base64.b64decode(value) for name, value in secret["data"].items()}
    assert decoded == {
        "host_key": b"fixture host key\n",
        "user_ca.pub": f"{USER_CA}\n".encode(),
        "principals": f"{EXE_A}\n".encode(),
    }


def _pod(change) -> dict:
    pod = copy.deepcopy(fixture()["pod"])
    change(pod["metadata"])
    return pod


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(name="swarm-exe-" + "b" * 32),
        lambda m: m.update(namespace="default"),
        lambda m: m.update(deletionTimestamp="2026-10-11T00:00:00Z"),
        lambda m: m["labels"].update({"swarm.agentihooks.io/controller-owner": "agentihooks-swarm-other"}),
        lambda m: m["labels"].update({"swarm.agentihooks.io/execution-id": "exe-" + "b" * 32}),
        lambda m: m["labels"].update({"swarm.agentihooks.io/generation": "1"}),
        lambda m: m.pop("labels"),
    ],
)
def test_only_the_live_attempt_pod_owns_a_terminal(change):
    pod = _pod(change)
    assert refusal(lambda: endpoints.objects(endpoint(), pod, IDENTITY, USER_CA, clients())) == (
        "pod_foreign",
        "the Pod is not this execution's live attempt",
    )


def test_a_pod_without_metadata_owns_no_terminal():
    assert refusal(lambda: endpoints.objects(endpoint(), {}, IDENTITY, USER_CA, clients()))[0] == "pod_foreign"


def _service(change) -> dict:
    manifest = endpoints.service(endpoint(), {})
    change(manifest["spec"])
    return manifest


IDENTITY_MISMATCH = "service selector is not this execution's identity"
WIDER = "policy admits clients beyond the selected terminal clients"


@pytest.mark.parametrize(
    ("change", "found"),
    [
        (lambda s: s.update(type="LoadBalancer"), ["service type LoadBalancer is not ClusterIP"]),
        (lambda s: s.pop("type"), ["service type None is not ClusterIP"]),
        (lambda s: s.update(externalIPs=["1.1.1.1"]), ["service sets externalIPs"]),
        (lambda s: s.update(externalName="x.example"), ["service sets externalName"]),
        (lambda s: s.update(loadBalancerIP="1.1.1.1"), ["service sets loadBalancerIP"]),
        (lambda s: s.update(loadBalancerSourceRanges=[]), ["service sets loadBalancerSourceRanges"]),
        (lambda s: s["ports"][0].update(nodePort=30022), ["service port 2222 is not the private terminal port"]),
        (lambda s: s["ports"][0].update(port=22), ["service port 22 is not the private terminal port"]),
        (lambda s: s["ports"][0].update(targetPort=22), ["service port 2222 is not the private terminal port"]),
        (lambda s: s["selector"].pop("swarm.agentihooks.io/generation"), [IDENTITY_MISMATCH]),
        (
            lambda s: s.update(type="NodePort", selector={}),
            ["service type NodePort is not ClusterIP", IDENTITY_MISMATCH],
        ),
    ],
)
def test_public_service_exposure_is_named(change, found):
    assert endpoints.public_exposure(endpoint(), _service(change)) == found


def test_a_service_without_a_spec_reads_as_unselected():
    assert endpoints.public_exposure(endpoint(), {"kind": "Service"}) == [
        "service type None is not ClusterIP",
        IDENTITY_MISMATCH,
    ]


def _policy(change) -> dict:
    manifest = endpoints.network_policy(endpoint(), {}, clients())
    change(manifest["spec"])
    return manifest


@pytest.mark.parametrize(
    ("change", "found"),
    [
        (lambda s: s.update(podSelector={}), ["policy selects another Pod"]),
        (lambda s: s["ingress"][0]["from"].append({"ipBlock": {"cidr": "0.0.0.0/0"}}), [WIDER]),
        (lambda s: s["ingress"][0].update({"from": []}), [WIDER]),
        (lambda s: s["ingress"][0].pop("from"), [WIDER]),
        (lambda s: s["ingress"][0]["from"][0].pop("podSelector"), [WIDER]),
        (lambda s: s["ingress"][0]["from"][0].update(podSelector={"matchLabels": {}}), [WIDER]),
        (lambda s: s["ingress"].append({"from": []}), [WIDER]),
    ],
)
def test_policy_exposure_beyond_the_terminal_clients_is_named(change, found):
    assert endpoints.public_exposure(endpoint(), _policy(change)) == found


def test_a_policy_without_rules_admits_nothing_extra():
    manifest = {"kind": "NetworkPolicy", "spec": {"podSelector": {"matchLabels": OWNER}}}
    assert endpoints.public_exposure(endpoint(), manifest) == []


def test_the_rendered_service_and_policy_are_private():
    for manifest in endpoints.objects(endpoint(), fixture()["pod"], IDENTITY, USER_CA, clients()):
        assert endpoints.public_exposure(endpoint(), manifest) == []


def test_other_kinds_are_not_exposure_checked():
    assert endpoints.public_exposure(endpoint(), {"kind": "Secret", "spec": {"type": "LoadBalancer"}}) == []


def test_a_publicly_exposed_render_is_refused(monkeypatch):
    original = endpoints.service

    def load_balancer(found, owner):
        manifest = original(found, owner)
        manifest["spec"].update(type="LoadBalancer", externalIPs=["1.1.1.1"])
        return manifest

    monkeypatch.setattr(endpoints, "service", load_balancer)
    assert refusal(lambda: endpoints.objects(endpoint(), fixture()["pod"], IDENTITY, USER_CA, clients())) == (
        "public_exposure",
        "service type LoadBalancer is not ClusterIP; service sets externalIPs",
    )


def test_the_pod_mounts_its_terminal_material_and_names_the_private_port():
    pod = fixture()["pod"]
    mounted = endpoints.with_terminal(pod, endpoint())
    assert mounted["spec"]["volumes"] == [
        {"name": "terminal", "secret": {"secretName": f"swarm-{EXE_A}-terminal", "defaultMode": 0o440}}
    ]
    assert mounted["spec"]["containers"][0]["volumeMounts"] == [
        {"name": "terminal", "mountPath": "/var/run/swarm/terminal", "readOnly": True}
    ]
    assert mounted["spec"]["containers"][0]["ports"] == [
        {"name": "herdr-ssh", "containerPort": 2222, "protocol": "TCP"}
    ]
    assert pod == fixture()["pod"]


def test_a_host_identity_is_a_fresh_ed25519_key_pair():
    first, second = HostIdentity.generate(), HostIdentity.generate()
    key = serialization.load_ssh_private_key(first.private, None)
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    assert isinstance(key, Ed25519PrivateKey)
    assert public.decode() == first.public
    assert first.public != second.public
    assert "fixture host key" not in repr(IDENTITY)


def test_known_hosts_pins_the_key_to_the_endpoint_host():
    assert IDENTITY.known_hosts(endpoint()) == (
        f"swarm-{EXE_A}-terminal.swarm-workers.svc.cluster.local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey\n"
    )


def _authority(seconds=None):
    ca = Ed25519PrivateKey.generate()
    now = fixture()["now"]

    def clock():
        return now + 0.9

    authority = TerminalAuthority(ca, clock) if seconds is None else TerminalAuthority(ca, clock, seconds)
    return ca, authority


def _client() -> str:
    key = Ed25519PrivateKey.generate()
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def _openssh(key) -> str:
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def test_the_user_ca_is_the_authority_public_key():
    ca, authority = _authority()
    assert authority.user_ca == _openssh(ca.public_key())


def test_a_certificate_names_only_the_scoped_execution(monkeypatch):
    bits = []
    monkeypatch.setattr(endpoints.secrets, "randbits", lambda n: bits.append(n) or 7)
    _, authority = _authority()
    public = _client()
    issued = authority.issue(scope(), public)
    certificate = serialization.load_ssh_public_identity(issued.encode())
    certificate.verify_cert_signature()
    now = fixture()["now"]
    assert issued.endswith("\n") and issued.count("\n") == 1
    assert bits == [64]
    assert (
        certificate.serial,
        certificate.type,
        certificate.key_id,
        certificate.valid_principals,
        certificate.valid_after,
        certificate.valid_before,
        certificate.critical_options,
        certificate.extensions,
    ) == (
        7,
        serialization.SSHCertificateType.USER,
        b"grant/lgr-" + b"c" * 32,
        [EXE_A.encode()],
        now - 30,
        now + 300,
        {},
        {},
    )
    assert _openssh(certificate.signature_key()) == authority.user_ca
    assert _openssh(certificate.public_key()) == public


def test_a_certificate_lifetime_is_the_authority_window():
    _, authority = _authority(seconds=60)
    certificate = serialization.load_ssh_public_identity(authority.issue(scope(), _client()).encode())
    assert certificate.valid_before == fixture()["now"] + 60


@pytest.mark.parametrize("client", ["", "not a key", "ssh-ed25519 !!!", "ssh-unknown AAAA"])
def test_a_certificate_needs_an_openssh_client_key(client):
    _, authority = _authority()
    assert refusal(lambda: authority.issue(scope(), client)) == (
        "client_key",
        "terminal client key is not an OpenSSH public key",
    )


def test_the_rendered_execution_pod_with_its_terminal_matches_the_golden_pod():
    pod = json.loads((FIXTURES / "pod-rendered.json").read_text())
    execution = SimpleNamespace(execution_id="exe-0f1e2d3c4b5a69788796a5b4c3d2e1f0", generation=3)
    found = Endpoint(operator_scope(OPERATOR, "fixture", execution), "swarm-pod-proof")
    assert endpoints.with_terminal(pod, found) == json.loads((FIXTURES / "herdr-terminal-pod.json").read_text())


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
