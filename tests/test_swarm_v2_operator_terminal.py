import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.gates.base import Who
from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import deployed
from scripts.swarm_v2.herdr import attach
from scripts.swarm_v2.herdr.attach import ClientHome
from scripts.swarm_v2.herdr.capabilities import Verdict
from scripts.swarm_v2.herdr.endpoints import HostIdentity, TerminalAuthority
from scripts.swarm_v2.kubernetes import operator_terminal
from scripts.swarm_v2.kubernetes.terminal import HostKeys
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.runtime.commands import Principal, Role
from tests.test_swarm_v2_kubernetes_terminal import (
    EXE_A,
    FIXTURES,
    NAME,
    Api,
    Worker,
    fixture,
    opener,
    refusal,
    registration,
)

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SWARM = "fixture-swarm"
MASTER = "master@a1b2c3-0001"
ENGINEER = "engineer@a1b2c3-0002"
NAMESPACE = fixture()["namespace"]


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    made = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    made.create(SwarmConfig(SWARM, "/repo", 1, 0))
    made.put_agent(SWARM, AgentRecord(MASTER, "master", "master", seat=f"master@{SWARM}"))
    made.put_agent(SWARM, AgentRecord(ENGINEER, "eng", "t1"))
    return made


def launched(store: RedisStore, identity: HostIdentity) -> None:
    objects, _, _ = opener(Api(), HostKeys(store), identity)
    assert objects.open(registration(), fixture()["pod"]) == ""


def ca_key(tmp_path: Path, key=None) -> Path:
    key = key or Ed25519PrivateKey.generate()
    path = tmp_path / "user-ca"
    encoded = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    )
    path.write_bytes(encoded)
    return path


def environ(tmp_path: Path, **changes) -> dict[str, str]:
    policy = json.loads((FIXTURES / "pod-policy.json").read_text())
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**policy, "namespace": NAMESPACE, "provider_accounts": []}))
    return {deployed.POLICY_ENV: str(path), operator_terminal.CA_KEY_ENV: str(ca_key(tmp_path)), **changes}


def lookup(host, port, type):  # noqa: A002
    return [(2, type, 6, "", ("10.43.12.7", port))]


def client(tmp_path: Path, presented: str) -> tuple[ClientHome, Worker]:
    home = ClientHome(tmp_path / "client")
    return home, Worker(home, presented)


def swarm_error(action) -> str:
    with pytest.raises(SwarmError) as caught:
        action()
    return str(caught.value)


def test_the_operator_attaches_through_the_controller_with_the_recorded_host_key(store, tmp_path):
    identity = HostIdentity.generate()
    launched(store, identity)
    home, run = client(tmp_path, identity.public)
    built = operator_terminal.terminal(environ(tmp_path), home, run, lookup)

    verdict = operator_terminal.attach(store, SWARM, OPERATOR, EXE_A, lambda: built)

    assert (verdict.target, verdict.incarnation, verdict.compatible) == (NAME, f"{EXE_A}/2", True)
    known = (home.terminals / "known_hosts" / NAME).read_text()
    assert known == f"{NAME}.{NAMESPACE}.svc.cluster.local {identity.public}\n"
    certificate = serialization.load_ssh_public_identity(
        (home.terminals / "certificates" / f"{NAME}-cert.pub").read_bytes()
    )
    assert (certificate.key_id, certificate.valid_principals) == (b"operator/operator", [EXE_A.encode()])
    assert [command[:5] for command in run.commands if command[:3] == ["herdr", "machine", "add"]] == [
        ["herdr", "machine", "add", "--label", NAME]
    ]


def test_the_terminal_is_built_from_the_pod_policy_namespace_and_the_operator_user_ca(tmp_path):
    key = Ed25519PrivateKey.generate()
    home, run = client(tmp_path, "")
    (tmp_path / "chosen").mkdir()
    settings = environ(tmp_path, **{operator_terminal.CA_KEY_ENV: str(ca_key(tmp_path / "chosen", key))})

    built = operator_terminal.terminal(settings, home, run, lookup)

    assert (built.home, built.namespace, built.run, built.lookup) == (home, NAMESPACE, run, lookup)
    assert built.authority.user_ca == TerminalAuthority(key).user_ca


def test_the_operator_session_is_the_operator_principal(store):
    assert operator_terminal.principal(store, SWARM, Who(), {}) == OPERATOR


def test_a_master_is_refused_before_the_terminal_is_built(store, tmp_path):
    launched(store, HostIdentity.generate())
    principal = operator_terminal.principal(store, SWARM, Who(MASTER, SWARM), {"AGENTIHOOKS_AGENT_NAME": MASTER})
    built = []

    assert principal == Principal(MASTER, Role.MASTER)
    assert refusal(lambda: operator_terminal.attach(store, SWARM, principal, EXE_A, lambda: built.append(1))) == (
        "unauthorized",
        "only the operator role opens a worker terminal",
    )
    assert built == []


def test_the_principal_reads_the_given_environment(store, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    named = {"AGENTIHOOKS_AGENT_NAME": ENGINEER}
    refused = f"only the operator or the master of swarm {SWARM} uses its swarm controls, and  is neither"

    assert swarm_error(lambda: operator_terminal.principal(store, SWARM, Who(), named)) == refused
    assert swarm_error(lambda: operator_terminal.command(store, SWARM, EXE_A, named, Who())) == refused


def test_an_engineer_session_has_no_terminal_principal(store):
    who = Who(ENGINEER, SWARM)

    assert swarm_error(
        lambda: operator_terminal.principal(store, SWARM, who, {"AGENTIHOOKS_AGENT_NAME": ENGINEER})
    ) == (f"only the operator or the master of swarm {SWARM} uses its swarm controls, and {ENGINEER} is neither")


def test_an_unknown_execution_is_refused_before_any_connection(store, tmp_path):
    home, run = client(tmp_path, HostIdentity.generate().public)
    built = operator_terminal.terminal(environ(tmp_path), home, run, lookup)

    assert refusal(lambda: operator_terminal.attach(store, SWARM, OPERATOR, EXE_A, lambda: built)) == (
        "host_key_unrecorded",
        f"no host key was recorded at launch for {EXE_A}",
    )
    assert run.commands == []
    assert not home.terminals.exists()


def test_the_terminal_needs_the_user_ca_key_and_the_pod_policy(tmp_path):
    home, run = client(tmp_path, "")
    settings = environ(tmp_path)
    garbage = tmp_path / "garbage"
    garbage.write_text("not a key\n")
    (tmp_path / "ecdsa").mkdir()
    foreign = ca_key(tmp_path / "ecdsa", ec.generate_private_key(ec.SECP256R1()))

    def build(**changes):
        return lambda: operator_terminal.terminal({**settings, **changes}, home, run, lookup)

    ca = operator_terminal.CA_KEY_ENV
    assert swarm_error(lambda: operator_terminal.terminal({}, home, run, lookup)) == (
        f"opening a worker terminal needs {deployed.POLICY_ENV}"
    )
    assert swarm_error(build(**{ca: ""})) == f"opening a worker terminal needs {ca}"
    assert swarm_error(build(**{ca: str(tmp_path / "missing")})) == (
        "the terminal user CA key is unreadable: No such file or directory"
    )
    assert swarm_error(build(**{ca: str(garbage)})) == "the terminal user CA key is not an OpenSSH private key"
    assert swarm_error(build(**{ca: str(foreign)})) == "the terminal user CA key must be Ed25519"


def test_the_terminal_subcommand_takes_only_the_execution():
    args = cli.build_parser().parse_args([SWARM, "terminal", EXE_A])

    assert (args.command, args.execution) == ("terminal", EXE_A)
    for extra in (["--target", NAME], ["--generation", "2"], ["--incarnation", f"{EXE_A}/2"], [NAME]):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([SWARM, "terminal", EXE_A, *extra])


@pytest.fixture
def operator_shell(monkeypatch, store, tmp_path):
    for name in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_LANE", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("hooks.context.broadcast.session_name", lambda pid: "")
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "client"))
    for key, value in environ(tmp_path).items():
        monkeypatch.setenv(key, value)
    return store


def test_the_command_opens_the_terminal_in_the_operator_home(operator_shell, monkeypatch, tmp_path, capsys):
    identity = HostIdentity.generate()
    launched(operator_shell, identity)
    home, run = client(tmp_path, identity.public)
    environments = []
    monkeypatch.setattr(operator_terminal, "runner", lambda environment: environments.append(environment) or run)
    monkeypatch.setattr(attach.socket, "getaddrinfo", lookup)

    assert cli.main([SWARM, "terminal", EXE_A]) == 0

    assert json.loads(capsys.readouterr().out) == {"target": NAME, "incarnation": f"{EXE_A}/2"}
    assert [(env["HOME"], env["PATH"].split(":")[0]) for env in environments] == [
        (str(home.root), str(home.ssh.parent))
    ]
    assert (home.terminals / "known_hosts" / NAME).read_text().split()[1:] == identity.public.split()


def test_the_command_refuses_an_unknown_execution(operator_shell, monkeypatch, tmp_path, capsys):
    _, run = client(tmp_path, HostIdentity.generate().public)
    monkeypatch.setattr(operator_terminal, "runner", lambda environment: run)

    assert cli.main([SWARM, "terminal", EXE_A]) == 1

    assert capsys.readouterr().err == (
        f"swarm: the worker terminal was refused: host_key_unrecorded: no host key was recorded at launch for {EXE_A}\n"
    )
    assert run.commands == []


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        (FileNotFoundError(2, "No such file or directory", "ssh"), "FileNotFoundError"),
        (subprocess.TimeoutExpired(["ssh"], 10), "TimeoutExpired"),
    ],
)
def test_the_command_reports_a_client_that_cannot_run(operator_shell, monkeypatch, capsys, failure, said):
    launched(operator_shell, HostIdentity.generate())

    def broken(command):
        raise failure

    monkeypatch.setattr(operator_terminal, "runner", lambda environment: broken)
    monkeypatch.setattr(attach.socket, "getaddrinfo", lookup)

    assert cli.main([SWARM, "terminal", EXE_A]) == 1

    assert capsys.readouterr().err == (
        f"swarm: the worker terminal was refused: unreachable: {NAME} terminal did not answer the connection: {said}\n"
    )


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        (
            FileNotFoundError(2, "No such file or directory", "herdr"),
            "FileNotFoundError: [Errno 2] No such file or directory: 'herdr'",
        ),
        (subprocess.TimeoutExpired(["herdr"], 10), "TimeoutExpired: Command '['herdr']' timed out after 10 seconds"),
    ],
)
def test_the_command_reports_a_herdr_client_that_cannot_run(operator_shell, monkeypatch, capsys, failure, said):
    launched(operator_shell, HostIdentity.generate())

    def broken(*args):
        raise failure

    monkeypatch.setattr(operator_terminal, "attach", broken)

    assert cli.main([SWARM, "terminal", EXE_A]) == 1

    assert capsys.readouterr().err == f"swarm: the worker terminal could not be opened: {said}\n"


def test_an_encrypted_user_ca_key_is_refused(monkeypatch, tmp_path):
    home, run = client(tmp_path, "")

    def encrypted(data, password):
        assert (data, password) == ((tmp_path / "user-ca").read_bytes(), None)
        raise TypeError("Password was not given but private key is encrypted.")

    monkeypatch.setattr(operator_terminal.serialization, "load_ssh_private_key", encrypted)

    assert swarm_error(lambda: operator_terminal.terminal(environ(tmp_path), home, run, lookup)) == (
        "the terminal user CA key is encrypted, and the command reads only an unencrypted key"
    )


def test_the_command_refuses_a_master_session(operator_shell, monkeypatch, capsys):
    launched(operator_shell, HostIdentity.generate())
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", MASTER)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SWARM)
    monkeypatch.setattr(operator_terminal, "runner", lambda environment: pytest.fail("no command may run"))

    assert cli.main([SWARM, "terminal", EXE_A]) == 1

    assert capsys.readouterr().err == (
        "swarm: the worker terminal was refused: unauthorized: only the operator role opens a worker terminal\n"
    )


def test_the_command_refuses_an_incompatible_herdr(operator_shell, monkeypatch, capsys):
    verdict = Verdict(NAME, f"{EXE_A}/2", ("herdr server lacks protocol=2", "herdr client lacks a"), {})
    monkeypatch.setattr(operator_terminal, "attach", lambda *args: verdict)

    assert cli.main([SWARM, "terminal", EXE_A]) == 1

    assert capsys.readouterr().err == (
        f"swarm: the worker terminal {NAME} is incompatible: herdr server lacks protocol=2; herdr client lacks a\n"
    )
