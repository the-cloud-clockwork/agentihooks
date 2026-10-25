import base64
import json
import struct
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.herdr import attach, endpoints
from scripts.swarm_v2.herdr.attach import ClientHome, Terminal
from scripts.swarm_v2.herdr.endpoints import (
    Clients,
    Endpoint,
    EndpointRefused,
    HostIdentity,
    Scope,
    TerminalAuthority,
    grant_scope,
)
from scripts.swarm_v2.herdr.upkeep import Attempt
from scripts.swarm_v2.kubernetes import terminal
from scripts.swarm_v2.kubernetes.client import AlreadyExists, ApiRefused
from scripts.swarm_v2.kubernetes.spec import PodSpecRefused, PodTemplate, canonical_digest, load_policy
from scripts.swarm_v2.kubernetes.terminal import Exposure, HostKeys, Launched, TerminalObjects, TerminalTemplate
from scripts.swarm_v2.runtime.commands import Principal, Role
from tests import sv2_hdr01_cases

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
EXE_A = "exe-" + "a" * 32
NAME = f"swarm-{EXE_A}-terminal"
USER_CA = endpoints.key_pair()[1]
OPERATOR = Principal("operator", Role.OPERATOR)


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-terminal.json").read_text())


def registration(**changes) -> Registration:
    return Registration(**{**fixture()["registration"], **changes})


def exposure() -> Exposure:
    return Exposure(Clients(**fixture()["clients"]), USER_CA)


def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


class Api:
    def __init__(self, failure=None, fail_at=0) -> None:
        self.namespace, self.created = fixture()["namespace"], []
        self.failure, self.fail_at = failure, fail_at

    def create_object(self, body):
        if self.failure is not None and len(self.created) == self.fail_at:
            raise self.failure
        self.created.append(body)
        return body


def opener(api, keys=None, identity=None):
    keys = keys or HostKeys(store())
    made = identity or HostIdentity.generate()
    return TerminalObjects(api, exposure(), keys, lambda: made), keys, made


def refusal(action) -> tuple[str, str]:
    with pytest.raises(EndpointRefused) as caught:
        action()
    return caught.value.reason, str(caught.value)


def test_exposure_on_creates_the_secret_service_and_policy_the_pod_owns():
    api = Api()
    objects, _, identity = opener(api)

    assert objects.open(registration(), fixture()["pod"]) == ""

    endpoint = endpoints.Endpoint(grant_scope(registration()), fixture()["namespace"])
    owner = {"apiVersion": "v1", "kind": "Pod", "name": f"swarm-{EXE_A}", "uid": fixture()["pod"]["metadata"]["uid"]}
    assert api.created == [
        endpoints.host_secret(endpoint, owner, identity, USER_CA),
        endpoints.service(endpoint, owner),
        endpoints.network_policy(endpoint, owner, Clients(**fixture()["clients"])),
    ]
    assert [body["kind"] for body in api.created] == ["Secret", "Service", "NetworkPolicy"]


def test_the_secret_holds_a_fresh_host_key_and_the_configured_user_ca():
    api = Api()
    objects, _, identity = opener(api)
    objects.open(registration(), fixture()["pod"])

    secret = api.created[0]
    assert secret["data"][endpoints.HOST_KEY] == endpoints._encode(identity.private)
    assert secret["data"][endpoints.USER_CA] == endpoints._encode(f"{USER_CA}\n")
    assert secret["metadata"]["name"] == NAME


def test_the_host_key_is_recorded_for_the_grant_execution_and_generation():
    api = Api()
    objects, keys, identity = opener(api)
    objects.open(registration(), fixture()["pod"])

    assert keys.launched("fixture-swarm", EXE_A) == Launched(EXE_A, 2, identity.public)


def test_the_terminal_scope_comes_from_the_verified_grant():
    api = Api()
    objects, _, _ = opener(api)
    objects.open(registration(), fixture()["pod"])

    labels = api.created[1]["spec"]["selector"]
    assert labels["swarm.agentihooks.io/execution-id"] == EXE_A
    assert labels["swarm.agentihooks.io/generation"] == "2"
    assert labels["swarm.agentihooks.io/controller-owner"] == "agentihooks-swarm-fixture-swarm"


def test_a_pod_the_grant_does_not_own_takes_no_terminal_object():
    api = Api()
    objects, keys, _ = opener(api)
    other = registration(execution_id="exe-" + "b" * 32)

    assert objects.open(other, fixture()["pod"]) == "terminal_pod_foreign"
    assert api.created == []
    assert refusal(lambda: keys.launched("fixture-swarm", other.execution_id))[0] == "host_key_unrecorded"


def test_a_grant_whose_scope_is_invalid_takes_no_terminal_object():
    api = Api()
    objects, _, _ = opener(api)

    assert objects.open(registration(generation=0), fixture()["pod"]) == "terminal_scope"
    assert api.created == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (AlreadyExists("swarm-x-terminal"), "terminal_exists"),
        (ApiRefused(403, "Forbidden"), "terminal_refused"),
        (ConnectionError("API server answered 503"), "terminal_unavailable"),
    ],
)
@pytest.mark.parametrize("fail_at", [0, 1, 2])
def test_a_refused_terminal_object_names_why_and_records_no_host_key(failure, reason, fail_at):
    api = Api(failure, fail_at)
    objects, keys, _ = opener(api)

    assert objects.open(registration(), fixture()["pod"]) == reason
    assert len(api.created) == fail_at
    assert refusal(lambda: keys.launched("fixture-swarm", EXE_A))[0] == "host_key_unrecorded"


def test_a_host_key_the_store_cannot_record_names_the_store():
    from redis.exceptions import ConnectionError as RedisDown

    class Down:
        def record(self, scope, host_public):
            raise RedisDown("down")

    api = Api()
    objects = TerminalObjects(api, exposure(), Down(), HostIdentity.generate)

    assert objects.open(registration(), fixture()["pod"]) == "terminal_unavailable"


def test_the_default_identity_is_the_host_key_generator():
    objects = TerminalObjects(Api(), exposure(), HostKeys(store()))

    assert objects.identity == HostIdentity.generate


def test_an_exposure_needs_an_openssh_user_ca():
    unsupported = "ssh-foo " + base64.b64encode(struct.pack(">I", 7) + b"ssh-foo").decode()

    assert refusal(lambda: Exposure(Clients(**fixture()["clients"]), "not a key")) == (
        "user_ca",
        "terminal user CA is not an OpenSSH public key",
    )
    assert refusal(lambda: Exposure(Clients(**fixture()["clients"]), unsupported))[0] == "user_ca"
    assert refusal(lambda: Exposure(Clients(**fixture()["clients"]), None))[0] == "user_ca"


def test_the_newest_launch_of_an_execution_names_its_generation_and_host_key():
    keys = HostKeys(store())
    keys.record(Scope("grant/x", "fixture-swarm", EXE_A, 2), "ssh-ed25519 AAAA-two")
    keys.record(Scope("grant/y", "fixture-swarm", EXE_A, 3), "ssh-ed25519 AAAA-three")

    assert keys.launched("fixture-swarm", EXE_A) == Launched(EXE_A, 3, "ssh-ed25519 AAAA-three")


def test_host_keys_are_kept_per_swarm():
    shared = store()
    HostKeys(shared).record(Scope("grant/x", "fixture-swarm", EXE_A, 2), "ssh-ed25519 AAAA")

    assert shared.redis.hgetall(shared.key("fixture-swarm", "terminal-host-keys")) == {
        EXE_A: json.dumps({"generation": 2, "host_public": "ssh-ed25519 AAAA"})
    }
    assert refusal(lambda: HostKeys(shared).launched("other", EXE_A)) == (
        "host_key_unrecorded",
        f"no host key was recorded at launch for {EXE_A}",
    )


def test_forgetting_a_host_key_drops_only_that_execution_in_that_swarm():
    shared = store()
    keys = HostKeys(shared)
    keys.record(Scope("grant/x", "fixture-swarm", EXE_A, 2), "ssh-ed25519 AAAA-a")
    keys.record(Scope("grant/y", "fixture-swarm", "exe-" + "b" * 32, 1), "ssh-ed25519 AAAA-b")
    keys.record(Scope("grant/z", "other", EXE_A, 1), "ssh-ed25519 AAAA-o")

    keys.forget("fixture-swarm", EXE_A)
    keys.forget("fixture-swarm", EXE_A)

    assert list(shared.redis.hgetall(shared.key("fixture-swarm", "terminal-host-keys"))) == ["exe-" + "b" * 32]
    assert keys.launched("other", EXE_A) == Launched(EXE_A, 1, "ssh-ed25519 AAAA-o")


EXE_B = "exe-" + "b" * 32
EXE_C = "exe-" + "c" * 32


def occupant(execution_id: str, generation: int, backend: str = "kubernetes") -> AgentRecord:
    return AgentRecord(
        "eng-1", "eng", "task", execution_id=execution_id, generation=generation, runtime_backend=backend
    )


def test_attachable_attempts_are_kubernetes_occupants_at_their_recorded_generation():
    keys = HostKeys(store())
    keys.record(Scope("grant/x", "fixture-swarm", EXE_A, 2), "ssh-ed25519 AAAA-a")
    keys.record(Scope("grant/y", "fixture-swarm", EXE_B, 1), "ssh-ed25519 AAAA-b")
    keys.record(Scope("grant/z", "fixture-swarm", EXE_C, 1), "ssh-ed25519 AAAA-c")
    keys.record(Scope("grant/w", "other", "exe-" + "d" * 32, 1), "ssh-ed25519 AAAA-d")
    occupants = [
        occupant(EXE_A, 2),
        occupant(EXE_B, 2),
        occupant(EXE_C, 1, "local"),
        occupant("exe-" + "d" * 32, 1),
        occupant("exe-" + "e" * 32, 1),
    ]

    assert keys.attempts("fixture-swarm", occupants, OPERATOR) == [
        Attempt(Scope("operator/operator", "fixture-swarm", EXE_A, 2), "ssh-ed25519 AAAA-a")
    ]


def test_no_occupant_means_no_attachable_attempt():
    keys = HostKeys(store())
    keys.record(Scope("grant/x", "fixture-swarm", EXE_A, 2), "ssh-ed25519 AAAA-a")

    assert keys.attempts("fixture-swarm", [], OPERATOR) == []


class Worker:
    def __init__(self, home: ClientHome, presented: str) -> None:
        self.home, self.presented = home, presented
        self.herdr = sv2_hdr01_cases.Runner(sv2_hdr01_cases.fixture()["server"])
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(list(command))
        if command[0] == attach.SSH:
            known = (self.home.terminals / "known_hosts" / NAME).read_text().split()[1:]
            if " ".join(known) != self.presented:
                return subprocess.CompletedProcess(command, 255, "", "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!")
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["herdr", "machine", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        if command[:3] == ["herdr", "machine", "add"]:
            return subprocess.CompletedProcess(command, 0, "Saved SSH machine p1. Remote server is ready.\n", "")
        forwarded = [*command[:2], sv2_hdr01_cases.TARGET, *command[3:]] if "--machine" in command else command
        return self.herdr(forwarded)


def client(tmp_path, presented: str) -> Terminal:
    home = ClientHome(tmp_path / "client")
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: fixture()["now"])

    def lookup(host, port, type):  # noqa: A002
        return [(2, type, 6, "", ("10.43.12.7", port))]

    return Terminal(home, authority, fixture()["namespace"], Worker(home, presented), lookup)


def launched(identity: HostIdentity) -> HostKeys:
    keys = HostKeys(store())
    objects = TerminalObjects(Api(), exposure(), keys, lambda: identity)
    assert objects.open(registration(), fixture()["pod"]) == ""
    return keys


def test_the_operator_attaches_with_the_host_key_recorded_at_launch(tmp_path):
    identity = HostIdentity.generate()
    keys = launched(identity)
    worker = client(tmp_path, identity.public)

    verdict = terminal.operator_attach(worker, keys, OPERATOR, "fixture-swarm", EXE_A)

    assert (verdict.target, verdict.incarnation, verdict.compatible) == (NAME, f"{EXE_A}/2", True)
    known = (worker.home.terminals / "known_hosts" / NAME).read_text()
    assert known == f"{NAME}.{fixture()['namespace']}.svc.cluster.local {identity.public}\n"


def test_an_attach_to_a_worker_with_a_changed_host_key_is_refused(tmp_path):
    keys = launched(HostIdentity.generate())
    worker = client(tmp_path, HostIdentity.generate().public)

    reason, message = refusal(lambda: terminal.operator_attach(worker, keys, OPERATOR, "fixture-swarm", EXE_A))

    assert (reason, message) == ("host_key_changed", f"{NAME} terminal refused the connection: host_key_changed")
    assert worker.terminal_endpoint_auth_failures() == {"host_key_changed": 1}
    assert not [command for command in worker.run.commands if command[:3] == ["herdr", "machine", "add"]]


def test_an_attach_without_a_recorded_host_key_is_refused_before_any_connection(tmp_path):
    keys = HostKeys(store())
    worker = client(tmp_path, HostIdentity.generate().public)

    assert refusal(lambda: terminal.operator_attach(worker, keys, OPERATOR, "fixture-swarm", EXE_A)) == (
        "host_key_unrecorded",
        f"no host key was recorded at launch for {EXE_A}",
    )
    assert worker.run.commands == []


def test_an_attach_reads_the_host_key_of_its_own_swarm_only(tmp_path):
    keys = launched(HostIdentity.generate())
    worker = client(tmp_path, HostIdentity.generate().public)

    assert refusal(lambda: terminal.operator_attach(worker, keys, OPERATOR, "other-swarm", EXE_A))[0] == (
        "host_key_unrecorded"
    )
    assert worker.run.commands == []


def test_only_the_operator_role_attaches(tmp_path):
    identity = HostIdentity.generate()
    keys = launched(identity)
    worker = client(tmp_path, identity.public)
    master = Principal("master@fixture", Role.MASTER)

    assert refusal(lambda: terminal.operator_attach(worker, keys, master, "fixture-swarm", EXE_A))[0] == (
        "unauthorized"
    )
    assert worker.run.commands == []


def policy() -> dict:
    return load_policy(FIXTURES / "pod-policy.json")


def launch() -> dict:
    return json.loads((FIXTURES / "pod-launch.json").read_text())


def test_exposure_on_gives_the_rendered_pod_the_terminal_mount_and_port():
    plain = PodTemplate(policy(), "agentihooks-swarm-fixture").render(launch())
    rendered = TerminalTemplate(PodTemplate(policy(), "agentihooks-swarm-fixture")).render(launch())

    execution = launch()["execution_id"]
    container = rendered.pod["spec"]["containers"][0]
    assert {"name": "terminal", "mountPath": endpoints.MATERIAL_DIR, "readOnly": True} in container["volumeMounts"]
    assert container["ports"] == [{"name": "herdr-ssh", "containerPort": 2222, "protocol": "TCP"}]
    assert {
        "name": "terminal",
        "secret": {"secretName": f"swarm-{execution}-terminal", "defaultMode": 0o440, "optional": True},
    } in rendered.pod["spec"]["volumes"]
    assert rendered.digest == canonical_digest(rendered.pod)
    assert rendered.digest != plain.digest


def test_exposure_off_renders_the_pod_unchanged():
    plain = PodTemplate(policy(), "agentihooks-swarm-fixture").render(launch())

    assert "ports" not in plain.pod["spec"]["containers"][0]
    assert all(volume["name"] != "terminal" for volume in plain.pod["spec"]["volumes"])


def test_the_terminal_template_refuses_what_the_pod_template_refuses():
    template = PodTemplate(policy(), "agentihooks-swarm-fixture")

    with pytest.raises(PodSpecRefused):
        TerminalTemplate(template).render({**launch(), "extra": 1})
    assert template.pod_spec_validation_failures_total() == {"fields": 1}


def test_attachable_lists_only_live_attempts_with_a_recorded_host_key():
    keys = HostKeys(store())
    for exe in (EXE_A, "exe-" + "b" * 32):
        keys.record(Scope("grant/x", "fixture-swarm", exe, 2), f"key-{exe[-1]}")
    live = ["exe-" + "c" * 32, "exe-" + "b" * 32, EXE_A, EXE_A]
    assert keys.attachable("fixture-swarm", live) == [
        Launched(EXE_A, 2, "key-a"),
        Launched("exe-" + "b" * 32, 2, "key-b"),
    ]
    assert keys.attachable("fixture-swarm", []) == []
    assert keys.attachable("other-swarm", [EXE_A]) == []


def test_operator_sync_registers_each_live_attempt_on_the_client(tmp_path):
    identity = HostIdentity.generate()
    keys = launched(identity)
    worker = client(tmp_path, identity.public)

    report = terminal.operator_sync(worker, keys, OPERATOR, "fixture-swarm", [EXE_A], enabled=True)

    assert (report.registered, report.refused, report.retired) == ({EXE_A: "p1"}, {}, [])
    assert worker.registrations.recorded(worker_endpoint()) == "p1"
    adds = [command for command in worker.run.commands if command[:3] == ["herdr", "machine", "add"]]
    assert adds == [
        ["herdr", "machine", "add", "--label", NAME, "--remote-session", "default", worker_endpoint().target]
    ]


def test_operator_sync_reports_an_attempt_whose_terminal_refuses(tmp_path):
    keys = launched(HostIdentity.generate())
    worker = client(tmp_path, HostIdentity.generate().public)

    report = terminal.operator_sync(worker, keys, OPERATOR, "fixture-swarm", [EXE_A], enabled=True)

    assert (report.registered, report.refused) == ({}, {EXE_A: "host_key_changed"})
    assert worker.terminal_endpoint_refusals() == {"host_key_changed": 1}
    assert not [command for command in worker.run.commands if command[:3] == ["herdr", "machine", "add"]]


def test_operator_sync_refuses_a_principal_without_the_operator_role(tmp_path):
    identity = HostIdentity.generate()
    keys = launched(identity)
    worker = client(tmp_path, identity.public)

    master = Principal("master@fixture", Role.MASTER)
    assert refusal(lambda: terminal.operator_sync(worker, keys, master, "fixture-swarm", [EXE_A], enabled=True)) == (
        "unauthorized",
        "only a launch grant or the operator role opens a worker terminal",
    )
    assert worker.run.commands == []


def test_stopping_automated_registration_leaves_existing_profiles_for_audit(tmp_path):
    identity = HostIdentity.generate()
    keys = launched(identity)
    worker = client(tmp_path, identity.public)
    terminal.operator_sync(worker, keys, OPERATOR, "fixture-swarm", [EXE_A], enabled=True)
    seen = len(worker.run.commands)
    owned = worker.registrations.store.path.read_text()

    report = terminal.operator_sync(worker, keys, OPERATOR, "fixture-swarm", [], enabled=False)

    assert report == terminal.Report()
    assert len(worker.run.commands) == seen
    assert worker.registrations.store.path.read_text() == owned
    assert worker.registrations.recorded(worker_endpoint()) == "p1"


def test_a_disabled_sync_touches_nothing_on_the_client(tmp_path):
    identity = HostIdentity.generate()
    keys = launched(identity)
    worker = client(tmp_path, identity.public)

    assert terminal.operator_sync(worker, keys, OPERATOR, "fixture-swarm", [EXE_A], enabled=False) == terminal.Report()
    assert worker.run.commands == []
    assert not worker.home.root.exists()


def worker_endpoint() -> Endpoint:
    return Endpoint(Scope("operator/x", "fixture-swarm", EXE_A, 2), fixture()["namespace"])
