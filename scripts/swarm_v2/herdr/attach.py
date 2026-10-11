import ipaddress
import json
import os
import socket
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm_v2.herdr.capabilities import Qualifier, Verdict, machine_probe
from scripts.swarm_v2.herdr.endpoints import PORT, Endpoint, EndpointRefused, Scope, TerminalAuthority

USER = "worker"
SSH = "/usr/bin/ssh"
REFUSALS = (("Host key verification failed", "host_key_changed"), ("Permission denied", "unauthorized"))

Run = Callable[[list[str]], subprocess.CompletedProcess]


def route(endpoint: Endpoint, lookup: Callable = socket.getaddrinfo) -> str:
    try:
        found = lookup(endpoint.host, PORT, type=socket.SOCK_STREAM)
    except OSError:
        found = []
    addresses = sorted({info[4][0] for info in found})
    if not addresses:
        raise EndpointRefused("no_route", f"{endpoint.host} has no cluster DNS route")
    if any(not ipaddress.ip_address(address).is_private for address in addresses):
        raise EndpointRefused("public_route", f"{endpoint.host} resolves outside the private network")
    return addresses[0]


@dataclass(frozen=True)
class ClientHome:
    root: Path
    user: str = USER
    port: int = PORT

    @property
    def config(self) -> Path:
        return self.root / "ssh_config"

    @property
    def ssh(self) -> Path:
        return self.root / "bin" / "ssh"

    @property
    def identity(self) -> Path:
        return self.root / "id_ed25519"

    def known_hosts(self, endpoint: Endpoint) -> Path:
        return self.root / "known_hosts" / endpoint.name

    def certificate(self, endpoint: Endpoint) -> Path:
        return self.root / "certificates" / f"{endpoint.name}-cert.pub"

    def block(self, endpoint: Endpoint) -> Path:
        return self.root / "endpoints" / f"{endpoint.name}.conf"

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        return {**base, "PATH": f"{self.ssh.parent}{os.pathsep}{base.get('PATH', os.defpath)}"}


def ssh_block(home: ClientHome, endpoint: Endpoint, address: str) -> str:
    lines = [
        f"Host {endpoint.host}",
        f"  HostName {address}",
        f"  Port {home.port}",
        f"  User {home.user}",
        f"  HostKeyAlias {endpoint.host}",
        f'  UserKnownHostsFile "{home.known_hosts(endpoint)}"',
        "  GlobalKnownHostsFile /dev/null",
        "  StrictHostKeyChecking yes",
        "  CheckHostIP no",
        "  UpdateHostKeys no",
        "  BatchMode yes",
        "  IdentitiesOnly yes",
        f'  IdentityFile "{home.identity}"',
        f'  CertificateFile "{home.certificate(endpoint)}"',
        "  ForwardAgent no",
        "  ClearAllForwardings yes",
    ]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.write_bytes(text if isinstance(text, bytes) else text.encode())
    staged.chmod(0o600)
    staged.replace(path)


def _classify(stderr: str) -> str:
    return next((reason for marker, reason in REFUSALS if marker in stderr), "unreachable")


class Terminal:
    def __init__(
        self,
        home: ClientHome,
        authority: TerminalAuthority,
        namespace: str,
        run: Run,
        lookup: Callable = socket.getaddrinfo,
    ) -> None:
        self.home, self.authority, self.namespace = home, authority, namespace
        self.run, self.lookup = run, lookup
        self.qualifier = Qualifier(machine_probe(run))
        self.failures: Counter = Counter()

    def _client_key(self) -> str:
        if self.home.identity.exists():
            key = serialization.load_ssh_private_key(self.home.identity.read_bytes(), None)
        else:
            key = Ed25519PrivateKey.generate()
            private = key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
            )
            _write(self.home.identity, private)
        public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        return public.decode()

    def register(self, scope: Scope, host_public: str) -> Endpoint:
        endpoint = Endpoint(scope, self.namespace)
        address = route(endpoint, self.lookup)
        home = self.home
        _write(home.certificate(endpoint), self.authority.issue(scope, self._client_key()))
        _write(home.known_hosts(endpoint), f"{endpoint.host} {host_public}\n")
        _write(home.block(endpoint), ssh_block(home, endpoint, address))
        _write(home.config, f'Include "{home.block(endpoint).parent}/*.conf"\n')
        _write(home.ssh, f'#!/bin/sh\nexec {SSH} -F "{home.config}" "$@"\n')
        home.ssh.chmod(0o700)
        return endpoint

    def _connect(self, endpoint: Endpoint) -> None:
        done = self.run([str(self.home.ssh), endpoint.host, "true"])
        if done.returncode != 0:
            reason = _classify(done.stderr or "")
            raise EndpointRefused(reason, f"{endpoint.name} terminal refused the connection: {reason}")

    def _save(self, endpoint: Endpoint) -> None:
        listed = self.run(["herdr", "machine", "list", "--json"])
        try:
            machines = json.loads(listed.stdout) if listed.returncode == 0 else []
        except ValueError:
            machines = []
        if any(machine.get("label") == endpoint.name for machine in machines if isinstance(machine, dict)):
            return
        done = self.run(["herdr", "machine", "add", "--label", endpoint.name, endpoint.target])
        if done.returncode != 0:
            raise EndpointRefused("machine_refused", f"herdr could not save {endpoint.name}")

    def _refuse(self, refused: EndpointRefused) -> NoReturn:
        self.failures[refused.reason] += 1
        raise refused

    def attach(self, scope: Scope, host_public: str) -> Verdict:
        try:
            endpoint = self.register(scope, host_public)
            self._connect(endpoint)
            self._save(endpoint)
        except EndpointRefused as refused:
            self._refuse(refused)
        return self.qualifier.verdict(endpoint.name, endpoint.incarnation)

    def terminal_endpoint_auth_failures(self) -> dict[str, int]:
        return dict(self.failures)
