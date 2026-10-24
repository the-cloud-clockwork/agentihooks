"""agentihooks swarm-terminal setup|install|sync: the operator machine's terminal attach files, placed by reference,
and the scheduled jobs that refresh them and keep every hive's published workers in the operator's herdr."""

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import yaml

from scripts import openbao_kv, operator_env
from scripts.gates.base import Who
from scripts.scheduler import Scheduler
from scripts.scheduler.service import ensure_running
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, SwarmError, redis_client
from scripts.swarm_v2.deployed import POLICY_ENV, pod_policy
from scripts.swarm_v2.herdr import publications
from scripts.swarm_v2.herdr.attach import ClientHome, Run
from scripts.swarm_v2.herdr.capabilities import runner
from scripts.swarm_v2.herdr.operator_service import Service
from scripts.swarm_v2.herdr.upkeep import Upkeep
from scripts.swarm_v2.kubernetes import operator_terminal
from scripts.swarm_v2.kubernetes.operator_terminal import CA_KEY_ENV, authority

if TYPE_CHECKING:
    from redis import Redis

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
LEGACY = (TIMER, UNIT, SYNC)
SYNC_SECONDS = 15
REFRESH_SECONDS = 900
OWNER = "swarm-terminal"
LIVE = ("active", "pending")
STATE_KEYS = {"due", "seen", "synced", "jump"}
REDIS_PATH = "k8s/agentihooks-swarm"
REDIS_FIELD = "redis_password"
REDIS_USER = "swarm"
ADDRESS_ENV = "AGENTIHOOKS_HIVE_REDIS_ADDRESS"
URL_FILE_ENV = "AGENTIHOOKS_HIVE_REDIS_URL_FILE"


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

    @property
    def redis_url(self) -> Path:
        return self.root / "hive-redis.url"

    @property
    def alarm(self) -> Path:
        return self.root / "alarm"

    @property
    def schedule(self) -> Path:
        return self.root / "schedule.json"

    @property
    def state(self) -> Path:
        return self.root / "sync-state.json"


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


def place_redis(placement: Placement, bao: openbao_kv.OpenBao, address: str) -> None:
    where = f"OpenBao {openbao_kv.MOUNT}/{REDIS_PATH}"
    try:
        values, _ = openbao_kv.read(bao, REDIS_PATH)
    except openbao_kv.OpenBaoError as error:
        raise SetupRefused(f"the hive Redis password could not be read from {where}: {error}") from None
    if not values.get(REDIS_FIELD):
        raise SetupRefused(f"the hive Redis password is missing from {where} field {REDIS_FIELD}")
    password = quote(values[REDIS_FIELD], safe="")
    url = "rediss://" + REDIS_USER + ":" + password + "@" + address + "/0\n"
    _stage(placement.redis_url, url.encode()).replace(placement.redis_url)


def settings(home: Path) -> dict[str, str]:
    lines = Placement(home / FOLDER).env.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def place(home: Path, bao: openbao_kv.OpenBao, antoncore: Path, address: str = "") -> dict:
    placement = Placement(home / FOLDER)
    fingerprint = place_ca_key(placement, bao)
    namespace = place_policy(placement, antoncore)
    env = f"{CA_KEY_ENV}={placement.ca_key}\n{POLICY_ENV}={placement.policy}\n"
    report = {
        "ca_key": str(placement.ca_key),
        "ca_fingerprint": fingerprint,
        "pod_policy": str(placement.policy),
        "namespace": namespace,
        "env_file": str(placement.env),
    }
    if address:
        place_redis(placement, bao, address)
        report["hive_redis"] = address
    if placement.redis_url.exists():
        env += f"{URL_FILE_ENV}={placement.redis_url}\n"
    _stage(placement.env, env.encode()).replace(placement.env)
    placement.alarm.unlink(missing_ok=True)
    return report


def setup(root: Path, environ: Mapping[str, str], antoncore: str | None = None) -> int:
    try:
        bao = openbao_kv.from_env(environ)
        if bao is None:
            raise SetupRefused(f"it needs {openbao_kv.URL_ENV} and {openbao_kv.TOKEN_ENV}")
        report = place(root, bao, antoncore_dir(environ, antoncore), environ.get(ADDRESS_ENV, ""))
    except SwarmError as refused:
        message = f"operator terminal setup refused: {refused}"
        print(message, file=sys.stderr, flush=True)
        alarm(root, environ, message)
        return 1
    Placement(root / FOLDER).state.unlink(missing_ok=True)
    print(json.dumps(report), flush=True)
    return 0


def transport(environ: Mapping[str, str], placed: Mapping[str, str]) -> Mapping[str, str]:
    name = publications.URL_ENVS[0]
    if environ.get(name) or URL_FILE_ENV not in placed:
        return environ
    try:
        url = Path(placed[URL_FILE_ENV]).read_text().strip()
    except OSError:
        raise publications.TransportRefused(f"the placed hive Redis URL {placed[URL_FILE_ENV]} is unreadable") from None
    return {**environ, name: url}


def _raised(placement: Placement, message: str) -> set[str]:
    try:
        entry = json.loads(placement.alarm.read_text())
    except (OSError, ValueError):
        return set()
    return set(entry["raised"]) if entry.get("message") == message else set()


def alarm(root: Path, environ: Mapping[str, str], message: str) -> None:
    placement = Placement(root / FOLDER)
    try:
        placed = settings(root)
    except OSError:
        placed = {}
    try:
        slugs = publications.connect(transport(environ, placed)).slugs()
    except (OSError, SwarmError):
        return
    raised = _raised(placement, message)
    ledger = LedgerClient()
    for slug in sorted(set(slugs) - raised):
        try:
            if ledger.followup(slug, message, needs_operator=True):
                raised.add(slug)
        except SwarmError:
            continue
    entry = {"message": message, "raised": sorted(raised)}
    _stage(placement.alarm, json.dumps(entry).encode()).replace(placement.alarm)


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
    return Service(Upkeep(terminal), connect or (lambda: publications.connect(transport(environ, placed))))


def _saved(placement: Placement) -> dict | None:
    try:
        saved = json.loads(placement.state.read_text())
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) and saved.keys() >= STATE_KEYS else None


def sync_pass(running: Service, root: Path, out=None) -> None:
    placement = Placement(root / FOLDER)
    saved = _saved(placement)
    if saved is not None:
        running.resume(saved)
    report = running.run_pass()
    _stage(placement.state, json.dumps(running.saved()).encode()).replace(placement.state)
    if report is not None:
        print(json.dumps(dataclasses.asdict(report)), file=out or sys.stdout, flush=True)


def sync_once(environ: Mapping[str, str], root: Path) -> int:
    sync_pass(service(environ, operator_terminal.client_home(environ), root), root)
    return 0


def _job(event: dict) -> tuple[Path, str]:
    job = json.loads(event["payload"])
    return Path(job["home"]), job["address"]


def _contained(command: str, work: Callable[[], object]) -> None:
    # A raised slot error stops this machine's scheduler runner for every job, so it is reported here instead.
    try:
        work()
    except SwarmError as refused:
        print(f"operator terminal {command} refused: {refused}", file=sys.stderr, flush=True)
    except Exception as error:
        print(f"operator terminal {command} failed: {type(error).__name__}: {error}", file=sys.stderr, flush=True)


def shell_environ() -> dict[str, str]:
    return {**os.environ, **operator_env.values(os.environ)}


def _setup_environ(address: str) -> dict[str, str]:
    environ = shell_environ()
    if address:
        environ[ADDRESS_ENV] = address
    return environ


def refresh(event: dict, _delivery_id: str, _redis: "Redis") -> None:
    root, address = _job(event)
    _contained("setup", lambda: setup(root, _setup_environ(address)))


def _awaiting_setup(placement: Placement) -> bool:
    return placement.alarm.exists() and not placement.env.exists()


def sync(event: dict, _delivery_id: str, _redis: "Redis") -> None:
    root, _ = _job(event)
    if _awaiting_setup(Placement(root / FOLDER)):
        return
    _contained("sync", lambda: sync_once(shell_environ(), root))


def outside_agent(who: Who) -> None:
    if who.name or who.swarm:
        raise SetupRefused(
            f"the operator terminal setup runs only outside agent sessions, and {who.name or who.swarm} is one"
        )


def antoncore_dir(environ: Mapping[str, str], flag: str | None) -> Path:
    return Path(flag or environ.get(ANTONCORE_ENV) or Path.home() / "dev" / "tcc-ecosystem" / "antoncore")


JOBS = (("refresh", refresh, REFRESH_SECONDS), ("sync", sync, SYNC_SECONDS))


def _payload(root: Path, address: str) -> str:
    return json.dumps({"home": str(root), "address": address}, sort_keys=True)


def _recorded(placement: Placement) -> dict:
    try:
        return json.loads(placement.schedule.read_text())
    except (OSError, ValueError):
        return {}


def _live(scheduler: Scheduler, entry_id: str) -> bool:
    try:
        return scheduler.get(entry_id).state in LIVE
    except KeyError:
        return False


def scheduled(root: Path, address: str, scheduler: Scheduler) -> bool:
    record = _recorded(Placement(root / FOLDER))
    return record.get("payload") == _payload(root, address) and all(
        _live(scheduler, record.get(name)) for name, _, _ in JOBS
    )


def schedule(root: Path, address: str, scheduler: Scheduler) -> None:
    if scheduled(root, address, scheduler):
        return
    placement, payload = Placement(root / FOLDER), _payload(root, address)
    entries = {}
    for name, slot, seconds in JOBS:
        entries[name] = scheduler.interval(owner=OWNER, payload=payload, every=seconds)
        entries[name].timeout.connect(slot)
    record = {"payload": payload, **{name: entry.id for name, entry in entries.items()}}
    _stage(placement.schedule, json.dumps(record).encode()).replace(placement.schedule)
    for entry in entries.values():
        entry.start()
    kept = {entry.id for entry in entries.values()}
    for entry in scheduler.owned(OWNER):
        if entry.id not in kept and entry.state in LIVE and _job(entry.record)[0] == root:
            entry.cancel()


def remove_units(unit_dir: Path, run: Callable = subprocess.run) -> None:
    present = [name for name in LEGACY if (unit_dir / name).exists()]
    if not present:
        return
    enabled = [name for name in (TIMER, SYNC) if name in present]
    try:
        if enabled:
            result = run(
                ["systemctl", "--user", "disable", "--now", *enabled], capture_output=True, text=True, timeout=30
            )
            if result.returncode:
                raise SetupRefused(f"systemctl could not disable {', '.join(enabled)}: {result.stderr.strip()}")
        for name in present:
            (unit_dir / name).unlink()
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupRefused(f"systemctl could not remove {', '.join(present)}: {type(error).__name__}") from None


def _scheduler() -> Scheduler:
    from redis.exceptions import RedisError

    try:
        return Scheduler(redis_client())
    except RedisError as error:
        raise SetupRefused(f"the scheduler Redis is unreachable: {type(error).__name__}") from None


def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def installed(root: Path, address: str = "", unit_dir: Path | None = None, scheduler: Scheduler | None = None) -> bool:
    if any(((unit_dir or _unit_dir()) / name).exists() for name in LEGACY):
        return False
    return scheduled(root, address, scheduler or _scheduler())


def install(
    root: Path,
    address: str = "",
    unit_dir: Path | None = None,
    run: Callable = subprocess.run,
    scheduler: Scheduler | None = None,
    start: Callable | None = None,
) -> None:
    schedule(root, address, scheduler or _scheduler())
    remove_units(unit_dir or _unit_dir(), run)
    (start or ensure_running)()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentihooks swarm-terminal", description="Operator terminal attach setup")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="Place the terminal user CA key and the Pod policy from their sources")
    setup.add_argument("--antoncore", help=f"antoncore checkout; defaults to {ANTONCORE_ENV}")
    installed = sub.add_parser(
        "install", help="Schedule setup and the herdr sync on the agentihooks scheduler, removing the old systemd units"
    )
    installed.add_argument("--redis-address", default="", help=f"hive Redis host:port setup composes, as {ADDRESS_ENV}")
    installed.add_argument(
        "--check", action="store_true", help="exit 0 only when both jobs are scheduled and no old systemd unit remains"
    )
    sub.add_parser("sync", help="Run one sync of every hive's published workers into the operator's herdr")
    return parser


def main(
    argv: list[str], environ: Mapping[str, str] | None = None, who: Who | None = None, home: Path | None = None
) -> int:
    args = _parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    root = home or Path(environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
    try:
        outside_agent(Who.from_env() if who is None else who)
    except SetupRefused as refused:
        print(f"operator terminal {args.command} refused: {refused}", file=sys.stderr)
        return 1
    if args.command == "setup":
        return setup(root, environ, args.antoncore)
    try:
        if args.command == "sync":
            return sync_once(environ, root)
        if args.check:
            return 0 if installed(root, args.redis_address) else 1
        install(root, args.redis_address)
        return 0
    except SwarmError as refused:
        print(f"operator terminal {args.command} refused: {refused}", file=sys.stderr)
        return 1
