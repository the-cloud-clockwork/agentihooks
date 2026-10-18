"""agentihooks swarm-jump prepare: the controller's SSH jump to worker terminals, recorded for the operator machine."""

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.swarm.store import PREFIX, RedisStore, SwarmError, connect
from scripts.swarm_v2.herdr.attach import USER, Jump
from scripts.swarm_v2.herdr.endpoints import JUMP_PRINCIPAL, PORT, Endpoint
from scripts.swarm_v2.herdr.upkeep import Attempt
from scripts.swarm_v2.kubernetes import operator_terminal

PRINCIPAL = JUMP_PRINCIPAL
KEY = f"{PREFIX}:terminal-jump"
STATE_ENV = "AGENTIHOOKS_TERMINAL_JUMP_STATE"


def sshd_config(state: Path, user: str) -> str:
    lines = [
        f"Port {PORT}",
        "ListenAddress 0.0.0.0",
        f"HostKey {state}/host_key",
        "PidFile none",
        "AuthorizedKeysFile none",
        f"TrustedUserCAKeys {state}/user_ca.pub",
        f"AuthorizedPrincipalsFile {state}/principals",
        f"AllowUsers {user}",
        "ForceCommand /bin/false",
        "PermitTTY no",
        "AllowTcpForwarding local",
        f"PermitOpen *:{PORT}",
        "AllowStreamLocalForwarding no",
        "AllowAgentForwarding no",
        "X11Forwarding no",
        "PermitTunnel no",
        "GatewayPorts no",
        "PermitRootLogin no",
        "PubkeyAuthentication yes",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "HostbasedAuthentication no",
        "UsePAM no",
        # The material sits on an emptyDir whose root is world writable, which StrictModes refuses.
        "StrictModes no",
        "PermitUserEnvironment no",
        "PrintMotd no",
        "LogLevel VERBOSE",
    ]
    return "\n".join(lines) + "\n"


def _write(path: Path, data: bytes) -> None:
    staged = path.with_name(f".{path.name}.tmp")
    staged.unlink(missing_ok=True)
    with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)
    staged.replace(path)


def _host_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        return serialization.load_ssh_private_key(path.read_bytes(), None)
    key = Ed25519PrivateKey.generate()
    _write(
        path,
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        ),
    )
    return key


def principals(hosts: Iterable[str]) -> str:
    opens = ",".join(f'permitopen="{host}:{PORT}"' for host in sorted(hosts))
    return f"restrict,port-forwarding,{opens} {PRINCIPAL}\n" if opens else ""


class Routes:
    def __init__(self, state: Path, namespace: str) -> None:
        self.state, self.namespace = state, namespace

    def publish(self, attempts: Iterable[Attempt]) -> None:
        hosts = (Endpoint(attempt.scope, self.namespace).host for attempt in attempts)
        _write(self.state / "principals", principals(hosts).encode())


def routes(environ: Mapping[str, str], namespace: str) -> Routes | None:
    state = environ.get(STATE_ENV)
    return Routes(Path(state), namespace) if state else None


def prepare(state: Path, user_ca: str) -> str:
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = _host_key(state / "host_key")
    _write(state / "user_ca.pub", f"{user_ca}\n".encode())
    _write(state / "principals", principals(()).encode())
    _write(state / "sshd_config", sshd_config(state, USER).encode())
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def record(store: RedisStore, jump: Jump) -> None:
    store.redis.set(KEY, json.dumps({"host": jump.host, "port": jump.port, "host_public": jump.host_public}))


def recorded(store: RedisStore) -> Jump | None:
    raw = store.redis.get(KEY)
    if not raw:
        return None
    entry = json.loads(raw)
    return Jump(entry["host"], entry["port"], entry["host_public"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentihooks swarm-jump", description="Controller SSH jump to worker terminals"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    prepared = sub.add_parser("prepare", help="Write the jump sshd material and record the jump for the operator")
    prepared.add_argument("--state", required=True, help="folder the jump sshd reads")
    return parser


def main(argv: list[str], environ: Mapping[str, str] | None = None, store: Callable[[], RedisStore] = connect) -> int:
    args = _parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        user_ca = operator_terminal.authority(environ).user_ca
        name = operator_terminal.JUMP_ENV
        if not environ.get(name):
            raise SwarmError(f"the controller jump needs {name}")
        address = operator_terminal.jump_address(environ[name])
        if address is None:
            raise SwarmError(f"{name} must be host:port")
        record(store(), Jump(*address, prepare(Path(args.state), user_ca)))
    except SwarmError as refused:
        print(f"controller jump refused: {refused}", file=sys.stderr)
        return 1
    return 0
