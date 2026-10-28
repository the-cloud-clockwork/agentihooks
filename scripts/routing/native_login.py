import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from scripts.routing import envs

COMMAND = ["auth", "status", "--text"]
STATUS = "Login method: Claude account"
STATUS_LINE = re.compile(r"Login method: Claude(?: \w+)? account")
PROOF = {"command": "claude auth status --text", "status": STATUS}
EXPIRED = "native Claude login is expired or missing; renew the native Claude login"
UNAVAILABLE = "native Claude login could not be checked"
REVOKED = "native Claude login was revoked or rejected by the provider; renew the native Claude login"
PROBE_TIMEOUT_S = 60
PROOF_NAME = "interactive-claude-status.json"


class LoginError(RuntimeError):
    pass


def check(environ: Mapping[str, str], account: str, run: Callable = subprocess.run) -> dict:
    try:
        status = run(
            [shutil.which("claude") or "claude", *COMMAND],
            env=envs.claude_interactive_child(environ, account),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LoginError(UNAVAILABLE) from exc
    if status.returncode or not any(STATUS_LINE.fullmatch(line.strip()) for line in status.stdout.splitlines()):
        raise LoginError(EXPIRED)
    return dict(PROOF)


def rejected(environ: Mapping[str, str], account: str, run: Callable = subprocess.run) -> bool:
    from scripts.claude_quota_balancer import _probe_command, auth_rejected

    try:
        probe = run(
            _probe_command("haiku", shutil.which("claude") or "claude"),
            env=envs.claude_interactive_child(environ, account),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return auth_rejected(probe.stdout)


def record(home: Path, slug: str, proof: dict) -> None:
    folder = home / slug
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with (folder / PROOF_NAME).open("x") as output:
            output.write(json.dumps(proof) + "\n")
    except FileExistsError:
        pass
