"""agentihooks swarm-terminal setup|install|sync: the operator machine's terminal attach files, placed by reference,
and the service that keeps every hive's published workers in the operator's herdr."""

import argparse
import base64
import dataclasses
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from scripts import openbao_kv
from scripts.gates.base import Who
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.deployed import POLICY_ENV, pod_policy
from scripts.swarm_v2.herdr import publications
from scripts.swarm_v2.herdr.attach import ClientHome, Run
from scripts.swarm_v2.herdr.capabilities import runner
from scripts.swarm_v2.herdr.operator_service import Service
from scripts.swarm_v2.herdr.upkeep import Upkeep
from scripts.swarm_v2.kubernetes import operator_terminal
from scripts.swarm_v2.kubernetes.operator_terminal import CA_KEY_ENV, authority

CA_PATH = "agentihooks-swarm/terminal-attach"
CA_FIELD = "user_ca"
POLICY_MANIFEST = Path("k8s", "manifests", "swarm", "policy", "pod-policy.yaml")
POLICY_KEY = "pod-policy.json"
ANTONCORE_ENV = "AGENTIHOOKS_ANTONCORE_DIR"
# Not a top level *.env: the hooks autoload ~/.agentihooks/*.env into every agent session.
FOLDER = "swarm-terminal"
UNIT = "agentihooks-swarm-terminal.service"
TIMER = "agentihooks-swarm-terminal.timer"
SYNC = "agentihooks-swarm-terminal-sync.service"
SYNC_SECONDS = 15


class SetupRefused(SwarmError):
    pass


@dataclass(frozen=True)
class Placement:
    root: Path

    @property
    def ca_key(self) -> Path:
        return self.root / CA_FIELD

    @property
    def policy(self) -> Path:
        return self.root / POLICY_KEY

    @property
    def env(self) -> Path:
        return self.root / "terminal.env"


def _stage(path: Path, data: bytes) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.unlink(missing_ok=True)
    with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(data)
    return staged


def _fingerprint(public: str) -> str:
    digest = hashlib.sha256(base64.b64decode(public.split()[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().removesuffix("=")


def place_ca_key(placement: Placement, bao: openbao_kv.OpenBao) -> str:
    where = f"OpenBao {openbao_kv.MOUNT}/{CA_PATH}"
    try:
        values, _ = openbao_kv.read(bao, CA_PATH)
    except openbao_kv.OpenBaoError as error:
        raise SetupRefused(f"the terminal user CA could not be read from {where}: {error}") from None
    if not values.get(CA_FIELD):
        raise SetupRefused(f"the terminal user CA is missing from {where} field {CA_FIELD}")
    staged = _stage(placement.ca_key, values[CA_FIELD].encode())
    try:
        user_ca = authority({CA_KEY_ENV: str(staged)}).user_ca
        staged.replace(placement.ca_key)
    except SwarmError as refused:
        raise SetupRefused(str(refused)) from None
    finally:
        staged.unlink(missing_ok=True)
    return _fingerprint(user_ca)


def place_policy(placement: Placement, antoncore: Path) -> str:
    source = antoncore / POLICY_MANIFEST
    try:
        manifest = yaml.safe_load(source.read_text())
    except OSError as error:
        raise SetupRefused(f"the Pod policy manifest {source} is unreadable: {error.strerror}") from None
    except (yaml.YAMLError, UnicodeDecodeError):
        raise SetupRefused(f"the Pod policy in {source} is not a YAML ConfigMap") from None
    data = manifest.get("data") if isinstance(manifest, dict) else None
    text = data.get(POLICY_KEY) if isinstance(data, dict) else None
    if not isinstance(text, str):
        raise SetupRefused(f"the Pod policy in {source} carries no ConfigMap data {POLICY_KEY}")
    staged = _stage(placement.policy, text.encode())
    try:
        namespace = pod_policy(str(staged))["namespace"]
        staged.replace(placement.policy)
    except SwarmError as refused:
        raise SetupRefused(f"the Pod policy in {source} is refused: {refused}") from None
    finally:
        staged.unlink(missing_ok=True)
    return namespace


def settings(home: Path) -> dict[str, str]:
    lines = Placement(home / FOLDER).env.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def place(home: Path, bao: openbao_kv.OpenBao, antoncore: Path) -> dict:
    placement = Placement(home / FOLDER)
    fingerprint = place_ca_key(placement, bao)
    namespace = place_policy(placement, antoncore)
    env = f"{CA_KEY_ENV}={placement.ca_key}\n{POLICY_ENV}={placement.policy}\n"
    _stage(placement.env, env.encode()).replace(placement.env)
    return {
        "ca_key": str(placement.ca_key),
        "ca_fingerprint": fingerprint,
        "pod_policy": str(placement.policy),
        "namespace": namespace,
        "env_file": str(placement.env),
    }


def service(
    environ: Mapping[str, str],
    client: ClientHome,
    root: Path,
    run: Run | None = None,
    connect: Callable[[], RedisStore] | None = None,
) -> Service:
    try:
        placed = settings(root)
    except OSError:
        raise SetupRefused("the terminal files are not placed: run agentihooks swarm-terminal setup first") from None
    try:
        terminal = operator_terminal.terminal(
            {**environ, **placed}, client, run or runner(client.environment(dict(environ)))
        )
    except SwarmError as refused:
        raise SetupRefused(str(refused)) from None
    return Service(Upkeep(terminal), connect or (lambda: publications.connect(environ)))


def serve(running: Service, passes: int | None = None, sleep: Callable = time.sleep, out=None) -> int:
    for count in itertools.count(1):
        report = running.run_pass()
        if report is not None:
            print(json.dumps(dataclasses.asdict(report)), file=out or sys.stdout, flush=True)
        if count == passes:
            return 0
        sleep(SYNC_SECONDS)


def outside_agent(who: Who) -> None:
    if who.name or who.swarm:
        raise SetupRefused(
            f"the operator terminal setup runs only outside agent sessions, and {who.name or who.swarm} is one"
        )


def antoncore_dir(environ: Mapping[str, str], flag: str | None) -> Path:
    return Path(flag or environ.get(ANTONCORE_ENV) or Path.home() / "dev" / "tcc-ecosystem" / "antoncore")


def unit(binary: str) -> str:
    return (
        "[Unit]\nDescription=agentihooks operator terminal setup\n\n"
        "[Service]\nType=oneshot\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm-terminal setup\n'
        f"ExecStartPost=-systemctl --user try-restart {SYNC}\n"
    )


def timer() -> str:
    return (
        "[Unit]\nDescription=agentihooks operator terminal setup, refreshed\n\n"
        "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=15min\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )


def sync_unit(binary: str) -> str:
    return (
        "[Unit]\nDescription=agentihooks operator herdr sync of hive workers\n\n"
        "[Service]\nType=simple\nRestart=always\nRestartSec=30\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm-terminal sync\n\n'
        "[Install]\nWantedBy=default.target\n"
    )


def install(binary: str | None = None, unit_dir: Path | None = None, run: Callable = subprocess.run) -> None:
    binary = binary or shutil.which("agentihooks")
    if not binary:
        raise SetupRefused("the agentihooks executable is not on PATH")
    unit_dir = unit_dir or Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    wanted = ((unit_dir / UNIT, unit(binary)), (unit_dir / TIMER, timer()), (unit_dir / SYNC, sync_unit(binary)))
    stale = [(path, text) for path, text in wanted if not path.exists() or path.read_text() != text]
    for path, text in stale:
        _stage(path, text.encode()).replace(path)
    try:
        if stale:
            run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30, check=True)
        results = {
            name: run(["systemctl", "--user", "enable", "--now", name], capture_output=True, text=True, timeout=30)
            for name in (TIMER, SYNC)
        }
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupRefused(f"systemctl could not enable {TIMER} and {SYNC}: {type(error).__name__}") from None
    failed = [f"{name}: {result.stderr.strip()}" for name, result in results.items() if result.returncode]
    if failed:
        raise SetupRefused(f"systemctl could not enable {'; '.join(failed)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks swarm-terminal", description="Operator terminal attach setup")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Place the terminal user CA key and the Pod policy from their sources")
    setup.add_argument("--antoncore", help=f"antoncore checkout; defaults to {ANTONCORE_ENV}")
    sub.add_parser("install", help="Write and enable the user timer that runs setup and the sync service")
    sub.add_parser("sync", help="Keep every hive's published workers in the operator's herdr")
    return parser


def main(
    argv: list[str], environ: Mapping[str, str] | None = None, who: Who | None = None, home: Path | None = None
) -> int:
    args = _parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    try:
        outside_agent(Who.from_env() if who is None else who)
        if args.command == "install":
            install()
            return 0
        root = home or Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
        if args.command == "sync":
            return serve(service(environ, operator_terminal.client_home(environ), root))
        bao = openbao_kv.from_env(environ)
        if bao is None:
            raise SetupRefused(f"it needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV}")
        report = place(root, bao, antoncore_dir(environ, args.antoncore))
    except SwarmError as refused:
        print(f"operator terminal {args.command} refused: {refused}", file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0
