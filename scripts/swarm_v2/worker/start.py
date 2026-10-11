"""The worker Pod entry: register with the controller, bootstrap the attempt folder and write the registration record
before the supervisor starts. Nothing is written until the launch record, grant and registration answer agree."""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path
from subprocess import SubprocessError
from urllib.parse import urlsplit

from scripts.swarm_v2 import supervision_runtime, worker_home
from scripts.swarm_v2.broadcast_bridge import API_URL, GRANT_NAME
from scripts.swarm_v2.supervision import SCHEMA_VERSION, LaunchRefused, checked_authority

TEMPLATES = Path("/opt/agentihooks/templates")
PROFILE = "default"
REGISTER = "/v2/executions/register"
RECORD = "registration.json"
BUSY = 503
REGISTER_ATTEMPTS = 10
RETRY_SECONDS = 2
TIMEOUT_SECONDS = 10
CONFIRMED = ("execution_id", "task_id", "grant_id")

Send = Callable[[str, str, dict], tuple[int, object]]


def post(url: str, grant: str, body: dict) -> tuple[int, object]:
    headers = {"Authorization": f"Bearer {grant}", "Content-Type": "application/json"}
    request = urllib.request.Request(url.rstrip("/") + REGISTER, json.dumps(body).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as answer:
            status, text = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (OSError, HTTPException):
        return BUSY, {}
    try:
        return status, json.loads(text)
    except ValueError:
        return status, {}


def _launch(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text())
    except (OSError, ValueError):
        raise LaunchRefused("missing or unreadable launch record") from None
    if not isinstance(spec, dict) or spec.get("schema_version") != SCHEMA_VERSION:
        raise LaunchRefused("unsupported launch")
    checked_authority(spec.get("authority"))
    if spec.get("harness") not in worker_home.TARGETS:
        raise LaunchRefused("unsupported harness")
    _control_url(spec.get("control_url"))
    return spec


def _control_url(url: object) -> None:
    refused = LaunchRefused("invalid control url")
    if not isinstance(url, str):
        raise refused
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        raise refused from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise refused


def _grant(folder: Path) -> str:
    try:
        grant = (folder / GRANT_NAME).read_text().strip()
    except OSError:
        grant = ""
    if not grant:
        raise LaunchRefused("missing launch grant")
    return grant


def register(spec: dict, grant: str, send: Send, sleep: Callable[[float], None]) -> dict:
    authority = spec["authority"]
    body = {"execution_id": authority["execution_id"], "generation": authority["generation"]}
    for attempt in range(REGISTER_ATTEMPTS):
        status, answer = send(spec["control_url"], grant, body)
        if status != BUSY or attempt == REGISTER_ATTEMPTS - 1:
            break
        sleep(RETRY_SECONDS)
    if status != 200:
        raise LaunchRefused(f"registration refused with status {status}")
    if not isinstance(answer, dict) or any(answer.get(key) != authority[key] for key in CONFIRMED):
        raise LaunchRefused("registration answer does not match the launch authority")
    return authority


def _private(path: Path, document: dict) -> None:
    staged = path.with_name(f"{path.name}.tmp")
    staged.unlink(missing_ok=True)
    with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(document, handle, sort_keys=True)
    staged.replace(path)


def prepare(
    attempt: Path,
    launch: Path,
    send: Send = post,
    sleep: Callable[[float], None] = time.sleep,
    boot: Callable[[worker_home.Request], dict] = worker_home.bootstrap,
) -> None:
    spec = _launch(launch)
    if attempt.name != spec["authority"]["execution_id"]:
        raise LaunchRefused("attempt folder does not name the launch execution")
    grant = _grant(launch.parent)
    authority = register(spec, grant, send, sleep)
    attempt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    boot(
        worker_home.Request(
            attempt.parent,
            attempt.name,
            TEMPLATES,
            {spec["harness"]: PROFILE},
            Path(sys.executable),
            {},
            {API_URL: spec["control_url"]},
            os.getuid(),
            os.getgid(),
        )
    )
    worker_home.store_grant(attempt, grant)
    _private(attempt / RECORD, authority)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("ERROR worker start requires attempt directory and launch record", file=sys.stderr)
        return 64
    os.umask(0o077)
    try:
        prepare(Path(args[0]), Path(args[1]))
    except (OSError, ValueError, KeyError, TypeError, SubprocessError) as refused:
        print(f"ERROR worker start refused: {refused}", file=sys.stderr)
        return 64
    print(json.dumps({"worker_start": "prepared", "attempt": Path(args[0]).name, "uid": os.getuid()}), flush=True)
    return supervision_runtime.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
