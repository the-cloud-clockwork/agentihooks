"""agentihooks hive add: a UI hive saves a machine in herdr and delivers its member credential over SSH."""

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        _run(
            run,
            ["herdr", "machine", "add", "--label", label, "--remote-session", REMOTE_SESSION, target],
            None,
            HERDR_TIMEOUT_S,
        )
    grant = auth.mint(redis, label, delivery.redis_url)
    try:
        _run(run, ["ssh", "-o", "BatchMode=yes", target, DELIVER], auth.env_text(delivery.url, grant), SSH_TIMEOUT_S)
        if delivery.ledger_ca:
            _run(run, ["ssh", "-o", "BatchMode=yes", target, DELIVER_CA], delivery.ledger_ca, SSH_TIMEOUT_S)
    except auth.HiveError:
        auth.revoke(redis, grant["id"])
        raise
    return grant


def _saved(run: Callable[..., subprocess.CompletedProcess], target: str) -> bool:
    listed = _run(run, list(LIST), None, HERDR_TIMEOUT_S)
    try:
        machines = json.loads(listed)
    except ValueError as exc:
        raise auth.HiveError(f"herdr machine list answered with output that is not JSON ({exc})") from exc
    return any(machine.get("target") == target for machine in machines)


def _run(run: Callable[..., subprocess.CompletedProcess], argv: list[str], stdin: str | None, timeout: int) -> str:
    try:
        done = run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise auth.HiveError(f"{argv[0]} failed ({exc})") from exc
    if done.returncode != 0:
        raise auth.HiveError(f"{argv[0]} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout
