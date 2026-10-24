import json
import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.herdr import attach, capabilities, deliveries
from scripts.swarm_v2.herdr.attach import ClientHome, Terminal
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused, TerminalAuthority, grant_scope
from tests import sv2_hdr01_cases

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
EXE_A = "exe-" + "a" * 32
NAME = f"swarm-{EXE_A}-terminal"
HOST = f"{NAME}.swarm-workers.svc.cluster.local"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey"
PROFILE = "dfc82003987b56c2d3094030456bbd11"


def saved(profile_id: str, label: str, target: str) -> dict:
    return {"id": profile_id, "label": label, "target": target, "session": "default", "enabled": True}


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-terminal.json").read_text())


def scope():
    return grant_scope(Registration(**fixture()["registration"]))


def endpoint() -> Endpoint:
    return Endpoint(scope(), "swarm-workers")


def lookup_of(addresses, seen=None):
    def lookup(host, port, type):  # noqa: A002
        if seen is not None:
            seen.append((host, port, type))
        return [(socket.AF_INET, type, 6, "", (address, port)) for address in addresses]

    return lookup


def no_dns(host, port, type):  # noqa: A002
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


class Runner:
    def __init__(self, home: ClientHome) -> None:
        self.home = home
        self.commands: list[list[str]] = []
        self.ssh = subprocess.CompletedProcess([], 0, "", "")
        self.listed = subprocess.CompletedProcess([], 0, "[]", "")
        self.added = subprocess.CompletedProcess([], 0, f"Saved SSH machine {PROFILE}. Remote server is ready.\n", "")
        self.herdr = sv2_hdr01_cases.Runner(sv2_hdr01_cases.fixture()["server"])

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(list(command))
        if command[0] == attach.SSH:
            return self.ssh
        if command[:3] == ["herdr", "machine", "list"]:
            return self.listed
        if command[:3] == ["herdr", "machine", "add"]:
            if self.added.returncode == 0:
                listing = json.dumps([saved(PROFILE, NAME, f"ssh://{HOST}")])
                self.listed = subprocess.CompletedProcess([], 0, listing, "")
            return self.added
        if command[:3] == ["herdr", "machine", "remove"]:
            self.listed = subprocess.CompletedProcess([], 0, "[]", "")
            return subprocess.CompletedProcess(command, 0, "", "")
        forwarded = [*command[:2], sv2_hdr01_cases.TARGET, *command[3:]] if "--machine" in command else command
        return self.herdr(forwarded)


def terminal(tmp_path, addresses=("10.43.12.7",), namespace="swarm-workers"):
    home = ClientHome(tmp_path / "client")
    runner = Runner(home)
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: fixture()["now"])
    return Terminal(home, authority, namespace, runner, lookup_of(addresses)), runner


def test_the_controller_router_journals_where_the_listing_and_health_pass_read(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "client"))
    built, _ = terminal(tmp_path)
    expected = tmp_path / "client" / ".ssh" / "swarm-terminals" / "deliveries.json"
    assert built.router().deliveries.path == deliveries.journal() == expected


def refusal(action) -> tuple[str, str]:
    with pytest.raises(EndpointRefused) as caught:
        action()
    return caught.value.reason, str(caught.value)


def test_a_private_route_resolves_to_its_lowest_address():
    seen = []
    assert attach.route(endpoint(), lookup_of(["10.43.12.9", "10.43.12.7", "10.43.12.9"], seen)) == "10.43.12.7"
    assert seen == [(HOST, 2222, socket.SOCK_STREAM)]


def test_a_private_ipv6_route_is_accepted():
    assert attach.route(endpoint(), lookup_of(["fd00::12"])) == "fd00::12"


def test_a_missing_cluster_dns_route_is_refused():
    assert refusal(lambda: attach.route(endpoint(), no_dns)) == ("no_route", f"{HOST} has no cluster DNS route")


def test_an_empty_answer_is_no_route():
    assert refusal(lambda: attach.route(endpoint(), lookup_of([])))[0] == "no_route"


def test_a_route_with_any_public_address_is_refused():
    assert refusal(lambda: attach.route(endpoint(), lookup_of(fixture()["routes"]["public"]))) == (
        "public_route",
        f"{HOST} resolves outside the private network",
    )


def test_the_default_lookup_is_the_system_resolver(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(attach.socket, "getaddrinfo", lookup_of(["10.43.12.8"], seen))
    assert attach.route(endpoint()) == "10.43.12.8"
    home = ClientHome(tmp_path)
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: fixture()["now"])
    assert Terminal(home, authority, "swarm-workers", Runner(home)).register(scope(), HOST_KEY) == endpoint()
    assert "  HostName 10.43.12.8" in home.block(endpoint()).read_text().splitlines()
    assert seen == [(HOST, 2222, socket.SOCK_STREAM)] * 2


def test_the_client_home_lays_out_one_file_set_per_endpoint(tmp_path):
    home = ClientHome(tmp_path)
    assert (home.user, home.port) == ("worker", 2222)
    terminals = tmp_path / ".ssh" / "swarm-terminals"
    assert (home.config, home.terminals, home.ssh, home.identity) == (
        tmp_path / ".ssh" / "config",
        terminals,
        terminals / "bin" / "ssh",
        terminals / "id_ed25519",
    )
    assert home.known_hosts(endpoint()) == terminals / "known_hosts" / NAME
    assert home.certificate(endpoint()) == terminals / "certificates" / f"{NAME}-cert.pub"
    assert home.block(endpoint()) == terminals / "endpoints" / f"{NAME}.conf"
    assert home.generation(endpoint()) == terminals / "generations" / NAME
    assert home.include == f'Include "{terminals}/endpoints/*.conf"\n'


def test_the_client_runs_herdr_in_its_own_home_with_its_ssh_first(tmp_path):
    home = ClientHome(tmp_path)
    bin_dir = tmp_path / ".ssh" / "swarm-terminals" / "bin"
    assert home.environment({"PATH": "/usr/bin", "HOME": "/h", "TERM": "dumb"}) == {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin",
        "HOME": str(tmp_path),
        "TERM": "dumb",
    }
    assert home.environment({}) == {"HOME": str(tmp_path), "PATH": f"{bin_dir}{os.pathsep}{os.defpath}"}


def test_the_ssh_wrapper_adds_the_client_config_only_when_herdr_passes_none(tmp_path):
    home = ClientHome(tmp_path)
    assert home.wrapper == (
        '#!/bin/sh\ncase " $* " in *" -F "*) exec /usr/bin/ssh "$@";; esac\n'
        f'exec /usr/bin/ssh -F "{tmp_path}/.ssh/config" "$@"\n'
    )


def test_the_ssh_block_pins_address_host_key_identity_and_certificate(tmp_path):
    home = ClientHome(tmp_path, "iamroot", 22506)
    assert attach.ssh_block(home, endpoint(), "10.43.12.7").splitlines() == [
        f"Host {HOST}",
        "  HostName 10.43.12.7",
        "  Port 22506",
        "  User iamroot",
        f"  HostKeyAlias {HOST}",
        f'  UserKnownHostsFile "{tmp_path}/.ssh/swarm-terminals/known_hosts/{NAME}"',
        "  GlobalKnownHostsFile /dev/null",
        "  StrictHostKeyChecking yes",
        "  CheckHostIP no",
        "  UpdateHostKeys no",
        "  BatchMode yes",
        "  IdentitiesOnly yes",
        f'  IdentityFile "{tmp_path}/.ssh/swarm-terminals/id_ed25519"',
        f'  CertificateFile "{tmp_path}/.ssh/swarm-terminals/certificates/{NAME}-cert.pub"',
        "  ForwardAgent no",
        "  ClearAllForwardings yes",
    ]
    assert attach.ssh_block(home, endpoint(), "10.43.12.7").endswith("yes\n")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_written_client_files_are_private_from_creation_and_replace_atomically(tmp_path, monkeypatch):
    target = tmp_path / "a" / "b" / "file"
    opened = []
    real_open = attach.os.open
    monkeypatch.setattr(
        attach.os, "open", lambda path, flags, mode: opened.append((flags, mode)) or real_open(path, flags, mode)
    )
    previous = os.umask(0)
    try:
        attach._write(target, "one")
        attach._write(target, b"two")
    finally:
        os.umask(previous)
    assert (target.read_text(), _mode(target), _mode(target.parent)) == ("two", 0o600, 0o700)
    assert opened == [(os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)] * 2
    assert sorted(path.name for path in target.parent.iterdir()) == ["file"]


def test_a_stale_staged_file_is_replaced(tmp_path):
    folder = tmp_path / "staged"
    folder.mkdir()
    target = folder / "file"
    (folder / ".file.tmp").write_text("stale")
    attach._write(target, "fresh")
    assert (target.read_text(), sorted(path.name for path in folder.iterdir())) == ("fresh", ["file"])


def test_register_writes_the_pinned_client_configuration(tmp_path):
    found, runner = terminal(tmp_path)
    home = found.home
    assert found.register(scope(), HOST_KEY) == endpoint()
    assert home.known_hosts(endpoint()).read_text() == f"{HOST} {HOST_KEY}\n"
    assert home.block(endpoint()).read_text() == attach.ssh_block(home, endpoint(), "10.43.12.7")
    assert home.config.read_text() == home.include
    assert home.ssh.read_text() == home.wrapper
    assert _mode(home.ssh) == 0o700
    certificate = serialization.load_ssh_public_identity(home.certificate(endpoint()).read_bytes())
    key = serialization.load_ssh_private_key(home.identity.read_bytes(), None).public_key()
    assert certificate.valid_principals == [EXE_A.encode()]
    assert certificate.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ) == key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    assert runner.commands == []


def test_an_existing_client_ssh_config_keeps_its_lines_after_one_include(tmp_path):
    found, _ = terminal(tmp_path)
    attach._write(found.home.config, "Host other\n  User me\n")
    found.register(scope(), HOST_KEY)
    found.register(scope(), HOST_KEY)
    assert found.home.config.read_text() == found.home.include + "Host other\n  User me\n"


def test_an_existing_ssh_config_is_edited_in_place_keeping_its_mode_and_link(tmp_path):
    found, _ = terminal(tmp_path)
    real = tmp_path / "operator-ssh-config"
    real.write_text("Host other\n")
    real.chmod(0o644)
    found.home.config.parent.mkdir(parents=True)
    found.home.config.symlink_to(real)
    found.register(scope(), HOST_KEY)
    assert (found.home.config.is_symlink(), _mode(real)) == (True, 0o644)
    assert real.read_text() == found.home.include + "Host other\n"


def test_a_new_ssh_config_is_private(tmp_path):
    found, _ = terminal(tmp_path)
    found.register(scope(), HOST_KEY)
    assert _mode(found.home.config) == 0o600


def test_register_records_the_newest_generation(tmp_path):
    found, _ = terminal(tmp_path)
    found.register(scope(), HOST_KEY)
    assert found.home.generation(endpoint()).read_text() == "2\n"


def test_an_older_generation_cannot_overwrite_the_newer_pin(tmp_path):
    found, runner = terminal(tmp_path)
    newer = grant_scope(Registration(**(fixture()["registration"] | {"generation": 3})))
    found.register(newer, "ssh-ed25519 NEWER")
    before = sorted(path.read_bytes() for path in found.home.terminals.rglob("*") if path.is_file())
    assert refusal(lambda: found.attach(scope(), HOST_KEY)) == (
        "stale_generation",
        f"{NAME} already serves generation 3",
    )
    after = sorted(path.read_bytes() for path in found.home.terminals.rglob("*") if path.is_file())
    assert (before, runner.commands) == (after, [])
    assert found.terminal_endpoint_refusals() == {"stale_generation": 1}
    assert found.terminal_endpoint_auth_failures() == {}


def test_the_same_generation_may_attach_again(tmp_path):
    found, _ = terminal(tmp_path)
    found.register(scope(), HOST_KEY)
    assert found.register(scope(), HOST_KEY) == endpoint()


def test_the_client_key_is_made_once_and_reused(tmp_path):
    found, _ = terminal(tmp_path)
    found.register(scope(), HOST_KEY)
    first = found.home.identity.read_bytes()
    found.register(scope(), HOST_KEY)
    assert found.home.identity.read_bytes() == first
    assert _mode(found.home.identity) == 0o600


def test_attach_connects_saves_the_machine_and_qualifies_its_incarnation(tmp_path):
    found, runner = terminal(tmp_path)
    verdict = found.attach(scope(), HOST_KEY)
    assert (verdict.target, verdict.incarnation, verdict.compatible) == (NAME, f"{EXE_A}/2", True)
    assert runner.commands[:3] == [
        ["/usr/bin/ssh", "-F", str(found.home.config), HOST, "true"],
        ["herdr", "machine", "list", "--json"],
        ["herdr", "machine", "add", "--label", NAME, "--remote-session", "default", f"ssh://{HOST}"],
    ]
    assert runner.commands[3] == ["herdr", "--machine", NAME, *capabilities.STATUS_SERVER]
    assert found.registrations.store.path == found.home.terminals / "registrations.json"
    assert [record.profile_id for record in found.registrations.store.load().values()] == [PROFILE]
    assert found.terminal_endpoint_auth_failures() == {}


def test_a_machine_this_client_saved_is_not_added_again(tmp_path):
    found, runner = terminal(tmp_path)
    found.attach(scope(), HOST_KEY)
    runner.listed = subprocess.CompletedProcess([], 0, json.dumps([saved(PROFILE, NAME, f"ssh://{HOST}")]), "")
    found.attach(scope(), HOST_KEY)
    assert sum(command[:3] == ["herdr", "machine", "add"] for command in runner.commands) == 1


def test_a_manual_machine_with_the_same_label_is_refused_not_adopted(tmp_path):
    found, runner = terminal(tmp_path)
    runner.listed = subprocess.CompletedProcess([], 0, json.dumps([saved("m1", NAME, "ssh://devbox")]), "")
    assert refusal(lambda: found.attach(scope(), HOST_KEY)) == (
        "label_collision",
        f"{NAME} is the label of a machine this controller does not own; "
        "audit it and remove it with herdr machine remove before this endpoint registers",
    )
    assert not any(command[:3] == ["herdr", "machine", "add"] for command in runner.commands)
    assert found.registrations.store.load() == {}
    assert found.terminal_endpoint_refusals() == {"label_collision": 1}


@pytest.mark.parametrize(
    ("listed", "message"),
    [
        (subprocess.CompletedProcess([], 1, json.dumps([{"label": NAME}]), ""), "herdr machine list failed"),
        (subprocess.CompletedProcess([], 0, "{", ""), "herdr machine list is not JSON"),
    ],
)
def test_an_unreadable_machine_list_refuses_the_save(tmp_path, listed, message):
    found, runner = terminal(tmp_path)
    runner.listed = listed
    assert refusal(lambda: found.attach(scope(), HOST_KEY)) == ("catalog_unreadable", message)
    assert not any(command[:3] == ["herdr", "machine", "add"] for command in runner.commands)


def test_a_refused_machine_save_is_counted(tmp_path):
    found, runner = terminal(tmp_path)
    runner.added = subprocess.CompletedProcess([], 1, "", "boom")
    assert refusal(lambda: found.attach(scope(), HOST_KEY)) == ("machine_refused", f"herdr could not save {NAME}")
    assert found.terminal_endpoint_refusals() == {"machine_refused": 1}
    assert found.terminal_endpoint_auth_failures() == {}


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        (
            "@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\r\nHost key verification failed.\r\n",
            "host_key_changed",
        ),
        ("No ED25519 host key is known for x.\r\nHost key verification failed.\r\n", "host_key_unverified"),
        ("worker@10.43.12.7: Permission denied (publickey).\r\n", "unauthorized"),
        ("ssh: connect to host 10.43.12.7 port 2222: Connection refused\r\n", "unreachable"),
        (None, "unreachable"),
    ],
)
def test_a_refused_connection_is_classified_counted_and_ends_the_attach(tmp_path, stderr, reason):
    found, runner = terminal(tmp_path)
    runner.ssh = subprocess.CompletedProcess([], 255, "", stderr)
    assert refusal(lambda: found.attach(scope(), HOST_KEY)) == (
        reason,
        f"{NAME} terminal refused the connection: {reason}",
    )
    assert runner.commands == [["/usr/bin/ssh", "-F", str(found.home.config), HOST, "true"]]
    assert found.terminal_endpoint_refusals() == {reason: 1}
    assert found.terminal_endpoint_auth_failures() == ({} if reason == "unreachable" else {reason: 1})


def test_failures_accumulate_by_reason(tmp_path):
    found, runner = terminal(tmp_path)
    runner.ssh = subprocess.CompletedProcess([], 255, "", "Permission denied")
    for _ in range(2):
        refusal(lambda: found.attach(scope(), HOST_KEY))
    found.lookup = no_dns
    refusal(lambda: found.attach(scope(), HOST_KEY))
    assert found.terminal_endpoint_refusals() == {"unauthorized": 2, "no_route": 1}
    assert found.terminal_endpoint_auth_failures() == {"unauthorized": 2}


def test_no_route_refuses_before_any_file_or_command(tmp_path):
    found, runner = terminal(tmp_path, addresses=())
    assert refusal(lambda: found.attach(scope(), HOST_KEY))[0] == "no_route"
    assert (runner.commands, found.home.root.exists()) == ([], False)
    assert found.terminal_endpoint_refusals() == {"no_route": 1}


def test_an_invalid_namespace_is_counted(tmp_path):
    found, runner = terminal(tmp_path, namespace="Bad")
    assert refusal(lambda: found.attach(scope(), HOST_KEY))[0] == "namespace"
    assert found.terminal_endpoint_refusals() == {"namespace": 1}


def test_an_incompatible_server_is_a_verdict_not_an_auth_failure(tmp_path):
    found, runner = terminal(tmp_path)
    runner.herdr = sv2_hdr01_cases.Runner(sv2_hdr01_cases.fixture()["missing_forwarding_server"])
    verdict = found.attach(scope(), HOST_KEY)
    assert "herdr server lacks surface_interest=true" in verdict.refusals
    assert runner.commands[-1] == ["herdr", "machine", "remove", PROFILE]
    assert found.registrations.store.load() == {}
    assert found.terminal_endpoint_auth_failures() == {}


@pytest.mark.parametrize("server", [None, "replaced"])
def test_a_server_that_cannot_be_qualified_loses_its_registration(tmp_path, server):
    found, runner = terminal(tmp_path)
    if server is None:
        runner.herdr = lambda command: subprocess.CompletedProcess(command, 2, "", "")
        raised = capabilities.Unreachable
    else:
        found.qualifier.retired[NAME] = {f"{EXE_A}/2"}
        raised = capabilities.Incompatible
    with pytest.raises(raised):
        found.attach(scope(), HOST_KEY)
    assert runner.commands[-1] == ["herdr", "machine", "remove", PROFILE]
    assert found.registrations.store.load() == {}


def test_a_verified_registration_survives_an_unreachable_probe(tmp_path):
    found, runner = terminal(tmp_path)
    found.attach(scope(), HOST_KEY)
    found.qualifier.verdicts.clear()
    runner.herdr = lambda command: subprocess.CompletedProcess(command, 2, "", "")
    with pytest.raises(capabilities.Unreachable):
        found.attach(scope(), HOST_KEY)
    assert not any(command[:3] == ["herdr", "machine", "remove"] for command in runner.commands)
    assert [record.profile_id for record in found.registrations.store.load().values()] == [PROFILE]


def test_a_compatible_attach_keeps_its_registration(tmp_path):
    found, runner = terminal(tmp_path)
    found.attach(scope(), HOST_KEY)
    assert not any(command[:3] == ["herdr", "machine", "remove"] for command in runner.commands)


EXE_B = "exe-" + "b" * 32


def scope_of(execution_id: str, generation: int):
    record = fixture()["registration"] | {"execution_id": execution_id, "generation": generation}
    return grant_scope(Registration(**record))


def test_sync_counts_every_attempt_it_refuses(tmp_path):
    found, runner = terminal(tmp_path)
    for execution_id in (EXE_A, EXE_B):
        found.register(scope_of(execution_id, 3), HOST_KEY)
    report = found.sync([(scope_of(EXE_A, 2), HOST_KEY), (scope_of(EXE_B, 2), HOST_KEY)], enabled=True)
    assert report.refused == {EXE_A: "stale_generation", EXE_B: "stale_generation"}
    assert found.terminal_endpoint_refusals() == {"stale_generation": 2}


def test_sync_reports_connection_and_registration_refusals_together(tmp_path):
    found, runner = terminal(tmp_path)
    runner.herdr = sv2_hdr01_cases.Runner(sv2_hdr01_cases.fixture()["missing_forwarding_server"])
    found.register(scope_of(EXE_A, 3), HOST_KEY)
    report = found.sync([(scope_of(EXE_A, 2), HOST_KEY), (scope_of(EXE_B, 1), HOST_KEY)], enabled=True)
    assert report.refused == {EXE_A: "stale_generation", EXE_B: "incompatible"}
    assert report.registered == {}


class SilentSsh(Runner):
    def __init__(self, home: ClientHome, failure: Exception) -> None:
        super().__init__(home)
        self.failure = failure

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        if command[0] == attach.SSH:
            self.commands.append(list(command))
            raise self.failure
        return super().__call__(command)


@pytest.mark.parametrize(
    ("failure", "named"),
    [
        (subprocess.TimeoutExpired([attach.SSH], 10), "TimeoutExpired"),
        (FileNotFoundError(2, "No such file or directory"), "FileNotFoundError"),
    ],
)
def test_a_connection_probe_that_never_answers_is_refused_as_unreachable(tmp_path, failure, named):
    found, _ = terminal(tmp_path)
    found.run = SilentSsh(found.home, failure)
    endpoint = Endpoint(scope_of(EXE_A, 1), "swarm-workers")

    assert refusal(lambda: found._connect(endpoint)) == (
        "unreachable",
        f"{endpoint.name} terminal did not answer the connection: {named}",
    )
    report = found.sync([(scope_of(EXE_A, 1), HOST_KEY)], enabled=True)
    assert report.refused == {EXE_A: "unreachable"}
    assert found.terminal_endpoint_refusals() == {"unreachable": 1}


@pytest.mark.parametrize("identity", [b"not a key\n", None])
def test_an_unreadable_client_identity_refuses_each_attempt(tmp_path, identity):
    found, runner = terminal(tmp_path)
    found.home.identity.parent.mkdir(parents=True, exist_ok=True)
    if identity is None:
        found.home.identity.mkdir()
    else:
        found.home.identity.write_bytes(identity)

    assert refusal(lambda: found.register(scope_of(EXE_A, 1), HOST_KEY)) == (
        "client_key",
        "terminal client identity is not a readable OpenSSH private key",
    )
    report = found.sync([(scope_of(EXE_A, 1), HOST_KEY), (scope_of(EXE_B, 1), HOST_KEY)], enabled=True)
    assert report.refused == {EXE_A: "client_key", EXE_B: "client_key"}
    assert found.terminal_endpoint_refusals() == {"client_key": 2}
    assert not any(command[0] == attach.SSH for command in runner.commands)


JUMP_HOST = "swarm.homeofanton.com"
JUMP_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJumpHostKey"


def jumped(tmp_path) -> ClientHome:
    return ClientHome(tmp_path / "client", jump=attach.Jump(JUMP_HOST, 2223, JUMP_KEY))


def test_a_jumped_block_keeps_the_cluster_name_behind_the_controller_jump(tmp_path):
    home = jumped(tmp_path)
    lines = attach.ssh_block(home, endpoint(), HOST).splitlines()
    assert lines[:3] == [f"Host {HOST}", f"  HostName {HOST}", f"  ProxyJump {attach.JUMP}"]
    assert lines[3:] == attach.ssh_block(ClientHome(tmp_path / "client"), endpoint(), HOST).splitlines()[2:]


def test_the_jump_block_pins_the_controller_host_key(tmp_path):
    home = jumped(tmp_path)
    terminals = tmp_path / "client" / ".ssh" / "swarm-terminals"
    assert attach.jump_block(home).splitlines() == [
        f"Host {attach.JUMP}",
        f"  HostName {JUMP_HOST}",
        "  Port 2223",
        "  User worker",
        f"  HostKeyAlias {JUMP_HOST}",
        f'  UserKnownHostsFile "{terminals}/known_hosts/{attach.JUMP}"',
        "  GlobalKnownHostsFile /dev/null",
        "  StrictHostKeyChecking yes",
        "  CheckHostIP no",
        "  UpdateHostKeys no",
        "  BatchMode yes",
        "  IdentitiesOnly yes",
        f'  IdentityFile "{terminals}/id_ed25519"',
        f'  CertificateFile "{terminals}/certificates/{attach.JUMP}-cert.pub"',
        "  ForwardAgent no",
    ]


def test_register_through_the_jump_needs_no_workstation_dns_and_pins_both_hosts(tmp_path):
    home = jumped(tmp_path)
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: fixture()["now"])
    found = Terminal(home, authority, "swarm-workers", Runner(home), no_dns)

    assert found.register(scope(), HOST_KEY) == endpoint()

    assert home.block(endpoint()).read_text() == attach.ssh_block(home, endpoint(), HOST)
    assert home.known_hosts(endpoint()).read_text() == f"{HOST} {HOST_KEY}\n"
    assert home.jump_config.read_text() == attach.jump_block(home)
    assert home.jump_known_hosts.read_text() == f"{JUMP_HOST} {JUMP_KEY}\n"
    assert _mode(home.jump_known_hosts) == 0o600


def test_without_a_jump_no_jump_files_are_written(tmp_path):
    found, _ = terminal(tmp_path)
    found.register(scope(), HOST_KEY)
    assert (found.home.jump_config.exists(), found.home.jump_known_hosts.exists()) == (False, False)


def test_register_through_the_jump_signs_a_jump_only_certificate(tmp_path):
    from scripts.swarm_v2.herdr import endpoints

    home = jumped(tmp_path)
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: fixture()["now"])
    found = Terminal(home, authority, "swarm-workers", Runner(home), no_dns)

    found.register(scope(), HOST_KEY)

    certificate = serialization.load_ssh_public_identity(home.jump_certificate.read_bytes())
    client = serialization.load_ssh_private_key(home.identity.read_bytes(), None).public_key()
    assert certificate.valid_principals == [endpoints.JUMP_PRINCIPAL.encode()]
    assert certificate.key_id == scope().actor.encode()
    assert certificate.extensions == {b"permit-port-forwarding": b""}
    endpoint_certificate = serialization.load_ssh_public_identity(home.certificate(endpoint()).read_bytes())
    assert endpoint_certificate.extensions == {}
    assert (
        certificate.valid_before - certificate.valid_after
        == endpoints.CERTIFICATE_SECONDS + endpoints.CLOCK_SKEW_SECONDS
    )
    assert certificate.public_key().public_bytes_raw() == client.public_bytes_raw()
    certificate.verify_cert_signature()
    assert certificate.signature_key().public_bytes_raw() == authority.key.public_key().public_bytes_raw()
    assert _mode(home.jump_certificate) == 0o600


def test_without_a_jump_no_jump_certificate_is_signed(tmp_path):
    found, _ = terminal(tmp_path)
    found.register(scope(), HOST_KEY)
    assert not found.home.jump_certificate.exists()
