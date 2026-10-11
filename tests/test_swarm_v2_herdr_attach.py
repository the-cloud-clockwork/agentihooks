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
from scripts.swarm_v2.herdr import attach, capabilities
from scripts.swarm_v2.herdr.attach import ClientHome, Terminal
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused, TerminalAuthority, grant_scope
from tests import sv2_hdr01_cases

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
EXE_A = "exe-" + "a" * 32
NAME = f"swarm-{EXE_A}-terminal"
HOST = f"{NAME}.swarm-workers.svc.cluster.local"
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureHostKey"


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
        self.added = subprocess.CompletedProcess([], 0, "saved", "")
        self.herdr = sv2_hdr01_cases.Runner(sv2_hdr01_cases.fixture()["server"])

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        self.commands.append(list(command))
        if command[0] == str(self.home.ssh):
            return self.ssh
        if command[:3] == ["herdr", "machine", "list"]:
            return self.listed
        if command[:3] == ["herdr", "machine", "add"]:
            return self.added
        forwarded = [*command[:2], sv2_hdr01_cases.TARGET, *command[3:]] if "--machine" in command else command
        return self.herdr(forwarded)


def terminal(tmp_path, addresses=("10.43.12.7",), namespace="swarm-workers"):
    home = ClientHome(tmp_path / "client")
    runner = Runner(home)
    authority = TerminalAuthority(Ed25519PrivateKey.generate(), lambda: fixture()["now"])
    return Terminal(home, authority, namespace, runner, lookup_of(addresses)), runner


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


def test_the_default_lookup_is_the_system_resolver():
    assert attach.route.__defaults__ == (socket.getaddrinfo,)
    assert Terminal.__init__.__defaults__ == (socket.getaddrinfo,)


def test_the_client_home_lays_out_one_file_set_per_endpoint(tmp_path):
    home = ClientHome(tmp_path)
    assert (home.user, home.port) == ("worker", 2222)
    assert (home.config, home.ssh, home.identity) == (
        tmp_path / "ssh_config",
        tmp_path / "bin/ssh",
        tmp_path / "id_ed25519",
    )
    assert home.known_hosts(endpoint()) == tmp_path / "known_hosts" / NAME
    assert home.certificate(endpoint()) == tmp_path / "certificates" / f"{NAME}-cert.pub"
    assert home.block(endpoint()) == tmp_path / "endpoints" / f"{NAME}.conf"


def test_the_client_path_puts_the_pinned_ssh_first(tmp_path):
    home = ClientHome(tmp_path)
    assert home.environment({"PATH": "/usr/bin", "HOME": "/h"}) == {
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}/usr/bin",
        "HOME": "/h",
    }
    assert home.environment({}) == {"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.defpath}"}


def test_the_ssh_block_pins_address_host_key_identity_and_certificate(tmp_path):
    home = ClientHome(tmp_path, "iamroot", 22506)
    assert attach.ssh_block(home, endpoint(), "10.43.12.7").splitlines() == [
        f"Host {HOST}",
        "  HostName 10.43.12.7",
        "  Port 22506",
        "  User iamroot",
        f"  HostKeyAlias {HOST}",
        f'  UserKnownHostsFile "{tmp_path}/known_hosts/{NAME}"',
        "  GlobalKnownHostsFile /dev/null",
        "  StrictHostKeyChecking yes",
        "  CheckHostIP no",
        "  UpdateHostKeys no",
        "  BatchMode yes",
        "  IdentitiesOnly yes",
        f'  IdentityFile "{tmp_path}/id_ed25519"',
        f'  CertificateFile "{tmp_path}/certificates/{NAME}-cert.pub"',
        "  ForwardAgent no",
        "  ClearAllForwardings yes",
    ]
    assert attach.ssh_block(home, endpoint(), "10.43.12.7").endswith("yes\n")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_written_client_files_are_private_and_replace_atomically(tmp_path):
    target = tmp_path / "a" / "b" / "file"
    attach._write(target, "one")
    attach._write(target, b"two")
    assert (target.read_text(), _mode(target), _mode(target.parent)) == ("two", 0o600, 0o700)
    assert sorted(path.name for path in target.parent.iterdir()) == ["file"]


def test_register_writes_the_pinned_client_configuration(tmp_path):
    found, runner = terminal(tmp_path)
    home = found.home
    assert found.register(scope(), HOST_KEY) == endpoint()
    assert home.known_hosts(endpoint()).read_text() == f"{HOST} {HOST_KEY}\n"
    assert home.block(endpoint()).read_text() == attach.ssh_block(home, endpoint(), "10.43.12.7")
    assert home.config.read_text() == f'Include "{home.root}/endpoints/*.conf"\n'
    assert home.ssh.read_text() == f'#!/bin/sh\nexec /usr/bin/ssh -F "{home.config}" "$@"\n'
    assert _mode(home.ssh) == 0o700
    certificate = serialization.load_ssh_public_identity(home.certificate(endpoint()).read_bytes())
    key = serialization.load_ssh_private_key(home.identity.read_bytes(), None).public_key()
    assert certificate.valid_principals == [EXE_A.encode()]
    assert certificate.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ) == key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    assert runner.commands == []


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
    assert (verdict.target, verdict.incarnation, verdict.compatible) == (NAME, EXE_A, True)
    assert runner.commands[:3] == [
        [str(found.home.ssh), HOST, "true"],
        ["herdr", "machine", "list", "--json"],
        ["herdr", "machine", "add", "--label", NAME, f"ssh://{HOST}"],
    ]
    assert runner.commands[3] == ["herdr", "--machine", NAME, *capabilities.STATUS_SERVER]
    assert found.terminal_endpoint_auth_failures() == {}


def test_a_saved_machine_is_not_added_again(tmp_path):
    found, runner = terminal(tmp_path)
    runner.listed = subprocess.CompletedProcess([], 0, json.dumps(["other", {"label": "x"}, {"label": NAME}]), "")
    found.attach(scope(), HOST_KEY)
    assert ["herdr", "machine", "add", "--label", NAME, f"ssh://{HOST}"] not in runner.commands


@pytest.mark.parametrize(
    "listed",
    [
        subprocess.CompletedProcess([], 1, json.dumps([{"label": NAME}]), ""),
        subprocess.CompletedProcess([], 0, "{", ""),
    ],
)
def test_an_unreadable_machine_list_still_adds_the_machine(tmp_path, listed):
    found, runner = terminal(tmp_path)
    runner.listed = listed
    found.attach(scope(), HOST_KEY)
    assert runner.commands[2] == ["herdr", "machine", "add", "--label", NAME, f"ssh://{HOST}"]


def test_a_refused_machine_save_is_counted(tmp_path):
    found, runner = terminal(tmp_path)
    runner.added = subprocess.CompletedProcess([], 1, "", "boom")
    assert refusal(lambda: found.attach(scope(), HOST_KEY)) == ("machine_refused", f"herdr could not save {NAME}")
    assert found.terminal_endpoint_auth_failures() == {"machine_refused": 1}


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("@@@ WARNING\r\nHost key verification failed.\r\n", "host_key_changed"),
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
    assert runner.commands == [[str(found.home.ssh), HOST, "true"]]
    assert found.terminal_endpoint_auth_failures() == {reason: 1}


def test_failures_accumulate_by_reason(tmp_path):
    found, runner = terminal(tmp_path)
    runner.ssh = subprocess.CompletedProcess([], 255, "", "Permission denied")
    for _ in range(2):
        refusal(lambda: found.attach(scope(), HOST_KEY))
    found.lookup = no_dns
    refusal(lambda: found.attach(scope(), HOST_KEY))
    assert found.terminal_endpoint_auth_failures() == {"unauthorized": 2, "no_route": 1}


def test_no_route_refuses_before_any_file_or_command(tmp_path):
    found, runner = terminal(tmp_path, addresses=())
    assert refusal(lambda: found.attach(scope(), HOST_KEY))[0] == "no_route"
    assert (runner.commands, found.home.root.exists()) == ([], False)
    assert found.terminal_endpoint_auth_failures() == {"no_route": 1}


def test_an_invalid_namespace_is_counted(tmp_path):
    found, runner = terminal(tmp_path, namespace="Bad")
    assert refusal(lambda: found.attach(scope(), HOST_KEY))[0] == "namespace"
    assert found.terminal_endpoint_auth_failures() == {"namespace": 1}


def test_an_incompatible_server_is_a_verdict_not_an_auth_failure(tmp_path):
    found, runner = terminal(tmp_path)
    runner.herdr = sv2_hdr01_cases.Runner(sv2_hdr01_cases.fixture()["missing_forwarding_server"])
    verdict = found.attach(scope(), HOST_KEY)
    assert "herdr server lacks surface_interest=true" in verdict.refusals
    assert found.terminal_endpoint_auth_failures() == {}
