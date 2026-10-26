"""agentihooks hive add: a UI hive saves a machine in herdr and delivers its member credential over SSH."""

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from scripts.hive import auth, registry
from scripts.hive.placement import YES

if TYPE_CHECKING:
    from redis import Redis

REMOTE_SESSION = "default"
NAME_PREFIX = "swarm-hive-"
MACHINES = f"{auth.PREFIX}:machines"
LIST = ("herdr", "machine", "list", "--json")
HERDR_TIMEOUT_S = 300
SSH_TIMEOUT_S = 60
# The remote shell resolves its own home; a worker hive's sshd sets AGENTIHOOKS_HOME.
DELIVER = (
    'umask 077 && home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}" && mkdir -p "$home" '
    '&& cat > "$home/hive.env.new" && mv "$home/hive.env.new" "$home/hive.env"'
)
DELIVER_CA = (
    'home="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}" && ca="${LEDGER_CA_FILE:-$home/ledger-ca.pem}" '
    '&& mkdir -p "$(dirname "$ca")" && cat > "$ca.new" && mv "$ca.new" "$ca"'
)


@dataclass(frozen=True)
class Delivery:
    url: str
    redis_url: str
    ledger_ca: str | None = None


def hive_name(ordinal: int) -> str:
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError("a hive needs a positive integer")
    return f"{NAME_PREFIX}{ordinal:03d}"


def name(redis: "Redis", machine: str, assign: bool = True, taken: Iterable[str] = ()) -> str:
    from redis.exceptions import WatchError

    with redis.pipeline() as pipe:
        while True:
            try:
                pipe.watch(MACHINES)
                named = pipe.hgetall(MACHINES)
                if machine in named:
                    return named[machine]
                used = {*named.values(), *taken}
                found = next(hive_name(ordinal) for ordinal in count(1) if hive_name(ordinal) not in used)
                if assign:
                    pipe.multi()
                    pipe.hset(MACHINES, machine, found)
                    pipe.execute()
                return found
            except WatchError:
                continue


def host(target: str) -> str:
    found = _hostname(target)
    if not found:
        raise auth.HiveError(f"the SSH target {target!r} names no host")
    return found


def _hostname(target: str) -> str | None:
    return urlsplit(target if "://" in target else f"ssh://{target}").hostname


def add(
    redis: "Redis",
    local_id: str,
    target: str,
    label: str,
    delivery: Delivery,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    _ui(redis, local_id)
    listed = _listed(run)
    _one_per_machine(listed, target, label)
    member = _member(redis, label)
    _keep(listed, target, label, run)
    if member:
        return {"id": member, "name": label}
    grant = auth.mint(redis, label, delivery.redis_url)
    try:
        run_checked(
            run, ["ssh", "-o", "BatchMode=yes", target, DELIVER], auth.env_text(delivery.url, grant), SSH_TIMEOUT_S
        )
        if delivery.ledger_ca:
            run_checked(run, ["ssh", "-o", "BatchMode=yes", target, DELIVER_CA], delivery.ledger_ca, SSH_TIMEOUT_S)
    except auth.HiveError:
        auth.revoke(redis, grant["id"])
        raise
    return grant


def _one_per_machine(listed: list[dict], target: str, label: str) -> None:
    machine = host(target)
    for saved in listed:
        if saved.get("label") != label and _hostname(saved.get("target") or "") == machine:
            raise auth.HiveError(
                f"{machine} already runs hive {saved.get('label')}; one machine runs one hive, "
                "so grow it with more max agents"
            )


def _member(redis: "Redis", label: str) -> str | None:
    for key in redis.scan_iter(match=f"{auth.PREFIX}:member:*"):
        if redis.hget(key, "name") == label:
            return key.rsplit(":", 1)[1]
    return None


def save(
    redis: "Redis", local_id: str, target: str, label: str, run: Callable[..., subprocess.CompletedProcess]
) -> None:
    _ui(redis, local_id)
    _keep(_listed(run), target, label, run)


def _ui(redis: "Redis", local_id: str) -> None:
    local = registry.show(redis, local_id)
    if local is None or local["ui"] != YES:
        raise auth.HiveError(f"hive {local_id} is headless; only a UI hive adds machines")


def _keep(listed: list[dict], target: str, label: str, run: Callable[..., subprocess.CompletedProcess]) -> None:
    if not any(saved.get("target") == target or saved.get("label") == label for saved in listed):
        run_checked(
            run,
            ["herdr", "machine", "add", "--label", label, "--remote-session", REMOTE_SESSION, target],
            None,
            HERDR_TIMEOUT_S,
        )


def _listed(run: Callable[..., subprocess.CompletedProcess]) -> list[dict]:
    listed = run_checked(run, list(LIST), None, HERDR_TIMEOUT_S)
    try:
        return json.loads(listed)
    except ValueError as exc:
        raise auth.HiveError(f"herdr machine list answered with output that is not JSON ({exc})") from exc


def forget(label: str, known_hosts: Path, run: Callable[..., subprocess.CompletedProcess]) -> list[str]:
    found = [machine for machine in _listed(run) if machine.get("label") == label]
    if not found:
        return [f"herdr holds no machine {label}"]
    done = []
    for machine in found:
        run_checked(run, ["herdr", "machine", "remove", machine["id"]], None, HERDR_TIMEOUT_S)
        done.append(f"removed herdr machine {label}")
        host = known_host(machine["target"])
        if known_hosts.is_file():
            run_checked(run, ["ssh-keygen", "-R", host, "-f", str(known_hosts)], None, SSH_TIMEOUT_S)
            done.append(f"removed the host key of {host}")
    return done


def known_host(target: str) -> str:
    parts = urlsplit(target if "://" in target else f"ssh://{target}")
    return f"[{parts.hostname}]:{parts.port}" if parts.port not in (None, 22) else parts.hostname


def run_checked(
    run: Callable[..., subprocess.CompletedProcess], argv: list[str], stdin: str | None, timeout: int
) -> str:
    try:
        done = run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise auth.HiveError(f"{argv[0]} failed ({exc})") from exc
    if done.returncode != 0:
        raise auth.HiveError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout
