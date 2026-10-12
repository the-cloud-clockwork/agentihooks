import ipaddress
import os
import socket
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from scripts.swarm_v2.herdr.capabilities import Incompatible, Qualifier, Unreachable, Verdict, machine_probe
from scripts.swarm_v2.herdr.catalog import Catalog
from scripts.swarm_v2.herdr.endpoints import (
    PORT,
    Endpoint,
    EndpointRefused,
    Scope,
    TerminalAuthority,
    key_pair,
    known_hosts,
)
from scripts.swarm_v2.herdr.reconcile import STORE, Registrations, Report, Store
from scripts.swarm_v2.herdr.target import DELIVERIES, Router

USER = "worker"
SSH = "/usr/bin/ssh"
REFUSALS = (
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "host_key_changed"),
    ("Host key verification failed", "host_key_unverified"),
    ("Permission denied", "unauthorized"),
)
AUTH_FAILURES = frozenset({"host_key_changed", "host_key_unverified", "unauthorized", "client_key"})
JUMP = "swarm-terminal-jump"

Run = Callable[[list[str]], subprocess.CompletedProcess]
Lookup = Callable[..., list[tuple]]


def route(endpoint: Endpoint, lookup: Lookup | None = None) -> str:
    try:
        found = (lookup or socket.getaddrinfo)(endpoint.host, PORT, type=socket.SOCK_STREAM)
    except OSError:
        found = []
    addresses = sorted({info[4][0] for info in found})
    if not addresses:
        raise EndpointRefused("no_route", f"{endpoint.host} has no cluster DNS route")
    if any(not ipaddress.ip_address(address).is_private for address in addresses):
        raise EndpointRefused("public_route", f"{endpoint.host} resolves outside the private network")
    return addresses[0]


@dataclass(frozen=True)
class Jump:
    host: str
    port: int
    host_public: str
    user: str = USER


@dataclass(frozen=True)
class ClientHome:
    """herdr's -F config includes $HOME/.ssh/config, but its no -F health check reads the passwd home's config."""

    root: Path
    user: str = USER
    port: int = PORT
    jump: Jump | None = None

    @property
    def jump_config(self) -> Path:
        return self.terminals / "endpoints" / f"{JUMP}.conf"

    @property
    def jump_known_hosts(self) -> Path:
        return self.terminals / "known_hosts" / JUMP

    @property
    def jump_certificate(self) -> Path:
        return self.terminals / "certificates" / f"{JUMP}-cert.pub"

    @property
    def config(self) -> Path:
        return self.root / ".ssh" / "config"

    @property
    def terminals(self) -> Path:
        return self.root / ".ssh" / "swarm-terminals"

    @property
    def journal(self) -> Path:
        return self.terminals / DELIVERIES

    @property
    def identity(self) -> Path:
        return self.terminals / "id_ed25519"

    def known_hosts(self, endpoint: Endpoint) -> Path:
        return self.terminals / "known_hosts" / endpoint.name

    def certificate(self, endpoint: Endpoint) -> Path:
        return self.terminals / "certificates" / f"{endpoint.name}-cert.pub"

    def generation(self, endpoint: Endpoint) -> Path:
        return self.terminals / "generations" / endpoint.name

    def block(self, endpoint: Endpoint) -> Path:
        return self.terminals / "endpoints" / f"{endpoint.name}.conf"

    @property
    def include(self) -> str:
        return f'Include "{self.terminals}/endpoints/*.conf"\n'

    @property
    def ssh(self) -> Path:
        return self.terminals / "bin" / "ssh"

    @property
    def wrapper(self) -> str:
        return f'#!/bin/sh\ncase " $* " in *" -F "*) exec {SSH} "$@";; esac\nexec {SSH} -F "{self.config}" "$@"\n'

    def environment(self, base: dict[str, str]) -> dict[str, str]:
        path = f"{self.ssh.parent}{os.pathsep}{base.get('PATH', os.defpath)}"
        return {**base, "HOME": str(self.root), "PATH": path}


def ssh_block(home: ClientHome, endpoint: Endpoint, address: str) -> str:
    lines = [
        f"Host {endpoint.host}",
        f"  HostName {address}",
        *([f"  ProxyJump {JUMP}"] if home.jump else []),
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


def jump_block(home: ClientHome) -> str:
    jump = home.jump
    lines = [
        f"Host {JUMP}",
        f"  HostName {jump.host}",
        f"  Port {jump.port}",
        f"  User {jump.user}",
        f"  HostKeyAlias {jump.host}",
        f'  UserKnownHostsFile "{home.jump_known_hosts}"',
        "  GlobalKnownHostsFile /dev/null",
        "  StrictHostKeyChecking yes",
        "  CheckHostIP no",
        "  UpdateHostKeys no",
        "  BatchMode yes",
        "  IdentitiesOnly yes",
        f'  IdentityFile "{home.identity}"',
        f'  CertificateFile "{home.jump_certificate}"',
        "  ForwardAgent no",
    ]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.unlink(missing_ok=True)
    with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(text if isinstance(text, bytes) else text.encode())
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
        lookup: Lookup | None = None,
    ) -> None:
        self.home, self.authority, self.namespace = home, authority, namespace
        self.run, self.lookup = run, lookup
        self.qualifier = Qualifier(machine_probe(run))
        self.registrations = Registrations(Catalog(run), Store(home.terminals / STORE))
        self.failures: Counter = Counter()

    def _client_key(self) -> str:
        if not self.home.identity.exists():
            private, public = key_pair()
            _write(self.home.identity, private)
            return public
        try:
            key = serialization.load_ssh_private_key(self.home.identity.read_bytes(), None)
        except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
            raise EndpointRefused(
                "client_key", "terminal client identity is not a readable OpenSSH private key"
            ) from None
        return (
            key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        )

    def register(self, scope: Scope, host_public: str) -> Endpoint:
        endpoint = Endpoint(scope, self.namespace)
        home = self.home
        recorded = home.generation(endpoint)
        if recorded.exists() and scope.generation < (newest := int(recorded.read_text())):
            raise EndpointRefused("stale_generation", f"{endpoint.name} already serves generation {newest}")
        address = endpoint.host if home.jump else route(endpoint, self.lookup)
        if home.jump:
            _write(home.jump_known_hosts, f"{home.jump.host} {home.jump.host_public}\n")
            _write(home.jump_certificate, self.authority.issue_jump(scope.actor, self._client_key()))
            _write(home.jump_config, jump_block(home))
        _write(recorded, f"{scope.generation}\n")
        _write(home.certificate(endpoint), self.authority.issue(scope, self._client_key()))
        _write(home.known_hosts(endpoint), known_hosts(endpoint, host_public))
        _write(home.block(endpoint), ssh_block(home, endpoint, address))
        if not home.config.exists():
            _write(home.config, home.include)
        elif home.include not in (current := home.config.read_text()):
            home.config.write_text(home.include + current)
        _write(home.ssh, home.wrapper)
        home.ssh.chmod(0o700)
        return endpoint

    def _connect(self, endpoint: Endpoint) -> None:
        try:
            done = self.run([SSH, "-F", str(self.home.config), endpoint.host, "true"])
        except (OSError, subprocess.SubprocessError) as error:
            message = f"{endpoint.name} terminal did not answer the connection: {type(error).__name__}"
            raise EndpointRefused("unreachable", message) from None
        if done.returncode != 0:
            reason = _classify(str(done.stderr))
            raise EndpointRefused(reason, f"{endpoint.name} terminal refused the connection: {reason}")

    def _refuse(self, refused: EndpointRefused) -> NoReturn:
        self.failures[refused.reason] += 1
        raise refused

    def attach(self, scope: Scope, host_public: str) -> Verdict:
        try:
            endpoint = self.register(scope, host_public)
            self._connect(endpoint)
            recorded = self.registrations.recorded(endpoint)
            fresh = self.registrations.register(endpoint) != recorded
            verdict = self._qualified(endpoint, fresh)
        except EndpointRefused as refused:
            self._refuse(refused)
        return verdict

    def sync(self, attachable: Iterable[tuple[Scope, str]], *, enabled: bool) -> Report:
        if not enabled:
            return Report()
        ready, refused = [], {}
        for scope, host_public in attachable:
            try:
                endpoint = self.register(scope, host_public)
                self._connect(endpoint)
                ready.append(endpoint)
            except EndpointRefused as refusal:
                self.failures[refusal.reason] += 1
                refused[scope.execution_id] = refusal.reason
        report = self.registrations.reconcile(ready, self.qualifier)
        report.refused |= refused
        return report

    def router(self) -> Router:
        registrations = self.registrations
        return Router(registrations.store, registrations.catalog, self.qualifier, self.run, self.home.journal)

    def _qualified(self, endpoint: Endpoint, fresh: bool) -> Verdict:
        try:
            verdict = self.qualifier.verdict(endpoint.name, endpoint.incarnation)
        except Unreachable:
            if fresh:
                self.registrations.withdraw(endpoint)
            raise
        except Incompatible:
            self.registrations.withdraw(endpoint)
            raise
        if not verdict.compatible:
            self.registrations.withdraw(endpoint)
        return verdict

    def terminal_endpoint_auth_failures(self) -> dict[str, int]:
        return {reason: count for reason, count in self.failures.items() if reason in AUTH_FAILURES}

    def terminal_endpoint_refusals(self) -> dict[str, int]:
        return dict(self.failures)
