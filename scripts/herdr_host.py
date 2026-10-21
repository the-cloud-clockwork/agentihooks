import contextlib
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.swarm.pane import typed_input

PLACEMENTS = ("tab", "split", "workspace")


class HerdrError(RuntimeError):
    pass


@dataclass(frozen=True)
class Placement:
    workspace_id: str
    tab_id: str
    pane_id: str
    terminal_id: str = ""


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


def server_socket(environ: dict[str, str]) -> str:
    return environ.get("HERDR_SOCKET_PATH") or str(Path.home() / ".config" / "herdr" / "herdr.sock")


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
    return Placement(
        workspace_id=pane["workspace_id"],
        tab_id=pane["tab_id"],
        pane_id=pane["pane_id"],
        terminal_id=pane.get("terminal_id", ""),
    )


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


def open_command(directory: Path, title: str, argv: list[str], workspace: str, environ: dict[str, str]) -> Placement:
    placed = open_pane(directory, title, {}, "tab", workspace, environ)
    _cli(["pane", "run", placed.pane_id, shlex.join(argv)], environ)
    return placed


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


def answer(pane_id: str, text: str, environ: dict[str, str], timeout_ms: int) -> bool:
    """Press Enter on a startup prompt once the pane shows its text; False when it never shows."""
    try:
        _cli(["pane", "wait-output", pane_id, "--match", text, "--timeout", str(timeout_ms)], environ)
        _cli(["pane", "send-keys", pane_id, "Enter"], environ)
    except (HerdrError, subprocess.TimeoutExpired):
        return False
    return True


READY = ("idle", "done")
READY_SETTLE_S = 1.5
RENAME_CONFIRM_S = 15.0
RENAME_POLL_S = 0.5
CLEAR_INPUT = "ctrl+u"


def agent_status(pane_id: str, environ: dict[str, str]) -> str:
    try:
        found = _cli(["agent", "get", pane_id], environ).get("agent", {})
    except (HerdrError, subprocess.TimeoutExpired):
        return "unknown"
    return found.get("agent_status") or "unknown"


def typed(pane_id: str, environ: dict[str, str]) -> str:
    return typed_input(_read(pane_id, environ))


def _read(pane_id: str, environ: dict[str, str]) -> str:
    """The pane's visible screen with its styles; herdr prints it raw, not as a JSON reply."""
    exe = binary()
    if exe is None:
        raise HerdrError("herdr is not installed")
    args = [exe, "pane", "read", pane_id, "--source", "visible", "--format", "ansi"]
    done = subprocess.run(args, capture_output=True, text=True, env=environ, timeout=30)
    if done.returncode != 0:
        raise HerdrError(f"herdr pane read: {done.stderr.strip()}")
    return done.stdout


def wait_ready(pane_id: str, environ: dict[str, str], timeout: float) -> bool:
    """True once the agent reads ready twice in a row: Codex reads idle for a moment before its startup turns busy."""
    deadline = time.monotonic() + timeout
    streak = 0
    while True:
        streak = streak + 1 if agent_status(pane_id, environ) in READY else 0
        if streak == 2:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(READY_SETTLE_S)


def name_session(
    pane_id: str, name: str, environ: dict[str, str], confirmed: Callable[[], bool], timeout: float = RENAME_CONFIRM_S
) -> str:
    """Codex has no launch option for a session name, so its own rename command is typed at its ready prompt."""
    try:
        why = busy(pane_id, environ)
    except (HerdrError, subprocess.TimeoutExpired):
        return "error"
    if why:
        return f"refused-{why}"
    try:
        _cli(["pane", "run", pane_id, f"/rename {name}"], environ)
        deadline = time.monotonic() + timeout
        while not confirmed():
            if time.monotonic() >= deadline:
                _clear(pane_id, environ)
                return "unconfirmed"
            time.sleep(RENAME_POLL_S)
    except (HerdrError, subprocess.TimeoutExpired):
        _clear(pane_id, environ)
        return "error"
    return "named"


def busy(pane_id: str, environ: dict[str, str]) -> str:
    status = agent_status(pane_id, environ)
    if status not in READY:
        return status
    return "typed" if typed(pane_id, environ) else ""


def _clear(pane_id: str, environ: dict[str, str]) -> None:
    with contextlib.suppress(HerdrError, subprocess.TimeoutExpired):
        _cli(["pane", "send-keys", pane_id, CLEAR_INPUT], environ)


def prompt(pane_id: str, text: str, environ: dict[str, str]) -> bool:
    try:
        _cli(["agent", "prompt", pane_id, text], environ)
    except (HerdrError, subprocess.TimeoutExpired):
        return False
    return True
