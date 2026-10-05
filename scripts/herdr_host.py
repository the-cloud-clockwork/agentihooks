import hashlib
import json
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

PLACEMENTS = ("tab", "split", "workspace")


class HerdrError(RuntimeError):
    pass


@dataclass(frozen=True)
class Placement:
    workspace_id: str
    tab_id: str
    pane_id: str


def binary() -> str | None:
    return shutil.which("herdr")


def _cli(args: list[str], environ: dict[str, str]) -> dict:
    exe = binary()
    if exe is None:
        raise HerdrError("herdr is not installed")
    done = subprocess.run([exe, *args], capture_output=True, text=True, env=environ, timeout=30)
    if done.returncode == 0 and not done.stdout.strip():
        return {}
    try:
        reply = json.loads(done.stdout or done.stderr)
    except ValueError as exc:
        raise HerdrError(f"herdr {' '.join(args[:2])}: {(done.stderr or done.stdout).strip()}") from exc
    if done.returncode != 0 or "error" in reply:
        message = reply.get("error", {}).get("message", "") if isinstance(reply.get("error"), dict) else ""
        raise HerdrError(f"herdr {' '.join(args[:2])}: {message or done.stderr.strip()}")
    return reply.get("result", {})


def server_running(environ: dict[str, str]) -> bool:
    try:
        _cli(["workspace", "list"], environ)
    except (HerdrError, subprocess.TimeoutExpired):
        return False
    return True


def ensure_server(environ: dict[str, str], timeout: float = 10.0) -> bool:
    """Start a detached herdr server when none answers; True when one was started."""
    if server_running(environ):
        return False
    exe = binary()
    if exe is None:
        raise HerdrError("herdr is not installed")
    subprocess.Popen(
        [exe, "server"],
        env=environ,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while not server_running(environ):
        if time.monotonic() >= deadline:
            raise HerdrError(f"herdr server did not start within {timeout:g}s")
        time.sleep(0.25)
    return True


def repo_label(directory: Path) -> str:
    """The repository name for a checkout or any of its worktrees; the directory name otherwise."""
    done = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    common = Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None
    return common.parent.name if common is not None else directory.name


def find_workspace(label: str, environ: dict[str, str]) -> str | None:
    for workspace in _cli(["workspace", "list"], environ).get("workspaces", []):
        if workspace.get("label") == label:
            return workspace["workspace_id"]
    return None


def _env_args(env: dict[str, str]) -> list[str]:
    return [arg for key, value in env.items() for arg in ("--env", f"{key}={value}")]


def _placed(pane: dict) -> Placement:
    return Placement(workspace_id=pane["workspace_id"], tab_id=pane["tab_id"], pane_id=pane["pane_id"])


def target_workspace(workspace: str, directory: Path, environ: dict[str, str]) -> tuple[str | None, str]:
    """(existing workspace id or None, label) for a new tab: the named crew workspace,
    else the caller's herdr workspace, else the repository's."""
    if workspace:
        return find_workspace(workspace, environ), workspace
    if environ.get("HERDR_WORKSPACE_ID"):
        return environ["HERDR_WORKSPACE_ID"], ""
    label = repo_label(directory)
    return find_workspace(label, environ), label


def open_pane(
    directory: Path,
    title: str,
    env: dict[str, str],
    placement: str,
    workspace: str,
    environ: dict[str, str],
) -> Placement:
    common = ["--cwd", str(directory), *_env_args(env), "--no-focus"]
    if placement == "split":
        caller = environ.get("HERDR_PANE_ID")
        if not caller:
            raise HerdrError("--placement split needs a caller inside a herdr pane (HERDR_PANE_ID)")
        return _placed(_cli(["pane", "split", caller, "--direction", "right", *common], environ)["pane"])
    workspace_id, label = target_workspace(workspace, directory, environ)
    if placement == "workspace" or workspace_id is None:
        created = _cli(["workspace", "create", "--label", label or title, *common], environ)
        return _placed(created["root_pane"])
    created = _cli(["tab", "create", "--workspace", workspace_id, "--label", title, *common], environ)
    return _placed(created["root_pane"])


def run(pane_id: str, launcher: Path, environ: dict[str, str]) -> None:
    _cli(["pane", "run", pane_id, f"exec {shlex.quote(str(launcher))}"], environ)


def agent_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-_")
    cleaned = cleaned if cleaned[:1].isalpha() else f"a-{cleaned}"
    if len(cleaned) <= 32:
        return cleaned
    tail = "-".join(cleaned.split("-")[-2:])[-16:]
    digest = hashlib.sha1(cleaned.encode()).hexdigest()[:6]
    head = cleaned[: 32 - len(tail) - len(digest) - 2].rstrip("-_")
    return f"{head}-{digest}-{tail}"


def rename_agent(pane_id: str, name: str, environ: dict[str, str], timeout: float = 15.0) -> bool:
    """Name the agent once herdr has detected it in the pane; False when it never is."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            _cli(["agent", "rename", pane_id, agent_name(name)], environ)
            return True
        except (HerdrError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)
