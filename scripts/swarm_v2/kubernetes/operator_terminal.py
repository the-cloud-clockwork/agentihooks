import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.gates.base import Who
from scripts.swarm import clearance
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.deployed import POLICY_ENV, pod_policy
from scripts.swarm_v2.herdr.attach import ClientHome, Jump, Lookup, Run, Terminal
from scripts.swarm_v2.herdr.capabilities import Verdict, runner
from scripts.swarm_v2.herdr.endpoints import EndpointRefused, TerminalAuthority
from scripts.swarm_v2.kubernetes.terminal import HostKeys, operator_attach
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.runtime.commands import Principal, Role

CA_KEY_ENV = "AGENTIHOOKS_TERMINAL_CA_KEY"
JUMP_ENV = "AGENTIHOOKS_TERMINAL_JUMP"
JUMP_KEY_ENV = "AGENTIHOOKS_TERMINAL_JUMP_HOST_KEY"


def jump_address(value: str) -> tuple[str, int] | None:
    host, _, port = value.rpartition(":")
    valid = host and not any(char.isspace() for char in host) and port.isdigit() and 0 < int(port) < 65536
    return (host, int(port)) if valid else None


def jump(environ: Mapping[str, str]) -> Jump | None:
    if not environ.get(JUMP_ENV):
        return None
    address = jump_address(environ[JUMP_ENV])
    if not (address and environ.get(JUMP_KEY_ENV)):
        raise SwarmError(f"{JUMP_ENV} must be host:port with {JUMP_KEY_ENV} naming its host key")
    return Jump(*address, environ[JUMP_KEY_ENV])


def client_home(environ: Mapping[str, str]) -> ClientHome:
    return ClientHome(Path.home(), jump=jump(environ))


def principal(store: RedisStore, slug: str, who: Who, environ: Mapping[str, str]) -> Principal:
    name = clearance.holder(store, slug, who, environ)
    return OPERATOR if name == clearance.OPERATOR else Principal(name, Role.MASTER)


def _setting(environ: Mapping[str, str], name: str) -> str:
    if not environ.get(name):
        raise SwarmError(f"opening a worker terminal needs {name}")
    return environ[name]


def authority(environ: Mapping[str, str]) -> TerminalAuthority:
    try:
        key = serialization.load_ssh_private_key(Path(_setting(environ, CA_KEY_ENV)).read_bytes(), None)
    except OSError as error:
        raise SwarmError(f"the terminal user CA key is unreadable: {error.strerror}") from None
    except TypeError:
        raise SwarmError(
            "the terminal user CA key is encrypted, and the command reads only an unencrypted key"
        ) from None
    except (ValueError, UnsupportedAlgorithm):
        raise SwarmError("the terminal user CA key is not an OpenSSH private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SwarmError("the terminal user CA key must be Ed25519")
    return TerminalAuthority(key)


def terminal(environ: Mapping[str, str], home: ClientHome, run: Run, lookup: Lookup | None = None) -> Terminal:
    namespace = pod_policy(_setting(environ, POLICY_ENV))["namespace"]
    return Terminal(home, authority(environ), namespace, run, lookup)


def attach(
    store: RedisStore, slug: str, caller: Principal, execution_id: str, build: Callable[[], Terminal]
) -> Verdict:
    if caller.role is not Role.OPERATOR:
        raise EndpointRefused("unauthorized", "only the operator role opens a worker terminal")
    return operator_attach(build(), HostKeys(store), caller, slug, execution_id)


def command(store: RedisStore, slug: str, execution_id: str, environ: Mapping[str, str], who: Who) -> dict:
    caller = principal(store, slug, who, environ)
    home = client_home(environ)

    def build() -> Terminal:
        return terminal(environ, home, runner(home.environment(dict(environ))))

    try:
        verdict = attach(store, slug, caller, execution_id, build)
    except EndpointRefused as refused:
        raise SwarmError(f"the worker terminal was refused: {refused.reason}: {refused}") from None
    except (OSError, subprocess.SubprocessError) as error:
        raise SwarmError(f"the worker terminal could not be opened: {type(error).__name__}: {error}") from None
    if not verdict.compatible:
        raise SwarmError(f"the worker terminal {verdict.target} is incompatible: {'; '.join(verdict.refusals)}")
    return {"target": verdict.target, "incarnation": verdict.incarnation}
