"""The worker Pod entry: register with the controller, bootstrap the attempt folder and write the registration record
before the supervisor starts. Nothing is written until the launch record, grant and registration answer agree."""

import json
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict
from http.client import HTTPException
from pathlib import Path
from subprocess import SubprocessError
from urllib.parse import urlsplit

from scripts.swarm_v2 import filesystem, supervision_runtime, worker_home, workspaces
from scripts.swarm_v2.broadcast_bridge import API_URL, AUTHORIZATION, GRANT_NAME, JSON_HEADERS
from scripts.swarm_v2.checkpoints import runtime as checkpoint_runtime
from scripts.swarm_v2.checkpoints.restore import Restored
from scripts.swarm_v2.supervision import SCHEMA_VERSION, LaunchRefused, checked_authority

TEMPLATES = Path("/opt/agentihooks/templates")
BUNDLE = Path("/var/run/swarm/bundle")
REGISTER = "/v2/executions/register"
RECORD = "registration.json"
RESTORED = "restore.json"
BUSY = 503
REGISTER_ATTEMPTS = 10
RETRY_SECONDS = 2
TIMEOUT_SECONDS = 10
CONFIRMED = ("execution_id", "task_id", "grant_id")
PROJECT = re.compile(r"github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")

Send = Callable[[str, str, dict], tuple[int, object]]


from scripts.swarm_v2.worker import git_auth, lease_renewal
from scripts.swarm_v2.worker.lease_renewal import send as transport


def post(url: str, grant: str, body: dict) -> tuple[int, object]:
    return transport("POST", url.rstrip("/") + REGISTER, grant, body, TIMEOUT_SECONDS)


def report(spec: dict, grant: str, restored: Restored) -> None:
    path = f"/v1/executions/{spec['execution_id']}/restore-outcome"
    headers = {**JSON_HEADERS, AUTHORIZATION: f"Bearer {grant}"}
    request = urllib.request.Request(
        spec["control_url"].rstrip("/") + path, json.dumps(asdict(restored)).encode(), headers
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as answer:
            if answer.status != 200:
                raise LaunchRefused("restore outcome was not accepted")
    except (OSError, HTTPException):
        raise LaunchRefused("restore outcome was not accepted") from None
    print(
        json.dumps(
            {
                "event": "restore_verified",
                "execution_id": spec["execution_id"],
                "recovery": restored.recovery,
                "checkpoint_id": restored.checkpoint_id,
                "watermark": restored.watermark,
            }
        ),
        flush=True,
    )


def _launch(path: Path, templates: Path, bundle: Path | None) -> dict:
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
    _profile(spec.get("profile"), templates, bundle)
    if "checkout" in spec:
        _project(spec["checkout"])
    return spec


def _project(checkout: object) -> None:
    project = checkout.get("project") if isinstance(checkout, dict) else None
    if not isinstance(project, str) or not PROJECT.fullmatch(project):
        raise LaunchRefused("launch record names no permitted project")
    if not isinstance(checkout.get("agent"), str):
        raise LaunchRefused("launch record names no agent for its checkout")


def checkout(attempt: Path, request: workspaces.Request) -> workspaces.Workspace:
    record = json.loads((attempt / "execution.json").read_text())
    return workspaces.prepare(filesystem.recorded(attempt, record), request)


def _checked_out(attempt: Path, spec: dict) -> dict:
    wanted, authority = spec.get("checkout"), spec["authority"]
    if wanted is None:
        return {}
    origin = f"https://{wanted['project']}.git"
    request = workspaces.Request(origin, authority["task_id"], authority["generation"], wanted["agent"])
    if os.environ.get("GITHUB_APP_ID"):
        git_auth.configure(wanted["project"])
    found = checkout(attempt, request)
    tree = {"project": wanted["project"], "branch": found.branch, "base": found.base, "base_commit": found.base_commit}
    return {"checkout": tree}


def _profile(profile: object, templates: Path, bundle: Path | None) -> None:
    if not isinstance(profile, str) or not profile:
        raise LaunchRefused("launch record names no profile")
    if not worker_home.NAME.fullmatch(profile):
        raise LaunchRefused("launch record names an invalid profile")
    if worker_home.folder(templates / profile):
        return
    if bundle is None:
        raise LaunchRefused(f"profile {profile} is not shipped in the worker image")
    if not worker_home.folder(worker_home.bundle_profiles(bundle) / profile):
        raise LaunchRefused(f"profile {profile} is not shipped in the worker image or the bundle")


def delivered(bundle: Path) -> Path | None:
    return bundle if worker_home.folder(bundle / worker_home.BUNDLE_TREE) else None


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
        raise LaunchRefused("missing launch grant") from None
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
    return answer


def _private(path: Path, document: dict) -> None:
    staged = path.with_name(f"{path.name}.tmp")
    staged.unlink(missing_ok=True)
    with os.fdopen(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(document, handle)
    staged.replace(path)


def _binding(attempt: Path, spec: dict, built: dict) -> None:
    from scripts.profiles import binding

    target, profile = spec["harness"], spec["profile"]
    report = attempt / "run" / "profile-binding.json"
    result = {"profile": profile, "harness": target}
    try:
        home = attempt / built["homes"][target] / f".{target}"
        rendered = binding.inspect(home, profile, target)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        _private(report, {**result, "state": "failed", "reason": str(exc)})
        raise LaunchRefused(f"profile binding failed: {exc}") from exc
    _private(report, {**result, "state": "passed", "rendered": rendered})
    print(json.dumps({"profile_binding": "passed", **result}), flush=True)


def _held(renewer: lease_renewal.Renewer) -> None:
    if renewer.lost is not None:
        raise LaunchRefused(f"admission lease lost: {renewer.lost}")


def _build(
    attempt: Path,
    spec: dict,
    renewer: lease_renewal.Renewer,
    boot: Callable[[worker_home.Request], dict],
    templates: Path,
    bundle: Path | None,
) -> dict:
    attempt.parent.mkdir(mode=0o700, exist_ok=True)
    record = boot(
        worker_home.Request(
            attempt.parent,
            attempt.name,
            templates,
            {spec["harness"]: spec["profile"]},
            Path(sys.executable),
            {},
            {API_URL: spec["control_url"]},
            os.getuid(),
            os.getgid(),
            bundle,
        )
    )
    prepared = _checked_out(attempt, spec)
    if spec.get("checkpoint_remote") is True:
        os.environ[checkpoint_runtime.REMOTE] = "true"
    _private(attempt / "launch.json", spec)
    worker_home.store_grant(attempt, renewer.grant)
    _held(renewer)
    restored = checkpoint_runtime.prepare(spec, attempt)
    if restored is not None:
        _held(renewer)
        if spec.get("checkpoint_remote") is True:
            report(spec, renewer.grant, restored)
        _private(attempt / RESTORED, asdict(restored))
    return {**record, **prepared}


def prepare(
    attempt: Path,
    launch: Path,
    send: Send = post,
    sleep: Callable[[float], None] = time.sleep,
    boot: Callable[[worker_home.Request], dict] = worker_home.bootstrap,
    templates: Path = TEMPLATES,
    bundle: Path = BUNDLE,
) -> dict:
    bundle = delivered(bundle)
    spec = _launch(launch, templates, bundle)
    if attempt.name != spec["authority"]["execution_id"]:
        raise LaunchRefused("attempt folder does not name the launch execution")
    grant = _grant(launch.parent)
    admission = register(spec, grant, send, sleep)
    lease = lease_renewal.Lease(attempt, spec["control_url"], admission, grant)
    renewer = lease_renewal.Renewer(lease, lease_renewal.send)
    with lease_renewal.kept(renewer):
        built = _build(attempt, spec, renewer, boot, templates, bundle)
        _binding(attempt, spec, built)
    _held(renewer)
    worker_home.store_grant(attempt, renewer.grant)
    _private(attempt / lease_renewal.ADMISSION, renewer.record())
    _private(attempt / RECORD, spec["authority"])
    return built


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("ERROR worker start requires attempt directory and launch record", file=sys.stderr)
        return 64
    os.umask(0o077)
    try:
        record = prepare(Path(args[0]), Path(args[1])) or {}
    except (OSError, ValueError, KeyError, TypeError, SubprocessError) as refused:
        print(f"ERROR worker start refused: {refused}", file=sys.stderr)
        return 64
    shown = {key: record[key] for key in ("chains", "checkout") if record.get(key)}
    print(json.dumps({"worker_start": "prepared", "attempt": Path(args[0]).name, "uid": os.getuid(), **shown}))
    return supervision_runtime.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
