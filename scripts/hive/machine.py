"""agentihooks hive add: a UI hive saves a machine in herdr and delivers its member credential over SSH."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from scripts.hive import auth, registry
from scripts.hive.placement import YES

if TYPE_CHECKING:
    from redis import Redis

REMOTE_SESSION = "default"
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


def add(
    redis: "Redis",
    local_id: str,
    target: str,
    label: str,
    delivery: Delivery,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    local = registry.show(redis, local_id)
    if local is None or local["ui"] != YES:
        raise auth.HiveError(f"hive {local_id} is headless; only a UI hive adds machines")
    if not _saved(run, target):
        call(
            run,
            ["herdr", "machine", "add", "--label", label, "--remote-session", REMOTE_SESSION, target],
            None,
            HERDR_TIMEOUT_S,
        )
    grant = auth.mint(redis, label, delivery.redis_url)
    try:
        call(run, ["ssh", "-o", "BatchMode=yes", target, DELIVER], auth.env_text(delivery.url, grant), SSH_TIMEOUT_S)
        if delivery.ledger_ca:
            call(run, ["ssh", "-o", "BatchMode=yes", target, DELIVER_CA], delivery.ledger_ca, SSH_TIMEOUT_S)
    except auth.HiveError:
        auth.revoke(redis, grant["id"])
        raise
    return grant


def _saved(run: Callable[..., subprocess.CompletedProcess], target: str) -> bool:
    return any(machine.get("target") == target for machine in _listed(run))


def _listed(run: Callable[..., subprocess.CompletedProcess]) -> list[dict]:
    listed = call(run, list(LIST), None, HERDR_TIMEOUT_S)
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
        call(run, ["herdr", "machine", "remove", machine["id"]], None, HERDR_TIMEOUT_S)
        done.append(f"removed herdr machine {label}")
        host = known_host(machine["target"])
        if known_hosts.is_file():
            call(run, ["ssh-keygen", "-R", host, "-f", str(known_hosts)], None, SSH_TIMEOUT_S)
            done.append(f"removed the host key of {host}")
    return done


def known_host(target: str) -> str:
    parts = urlsplit(target if "://" in target else f"ssh://{target}")
    return f"[{parts.hostname}]:{parts.port}" if parts.port not in (None, 22) else parts.hostname


def call(run: Callable[..., subprocess.CompletedProcess], argv: list[str], stdin: str | None, timeout: int) -> str:
    try:
        done = run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise auth.HiveError(f"{argv[0]} failed ({exc})") from exc
    if done.returncode != 0:
        raise auth.HiveError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout
