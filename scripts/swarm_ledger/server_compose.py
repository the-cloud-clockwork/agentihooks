import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from scripts.swarm_ledger import ledger_link, ports


def primary(root: Path) -> Path:
    marker = root / ".git"
    if not marker.is_file():
        return root
    return (root / marker.read_text().removeprefix("gitdir:").strip()).resolve().parents[2]


ROOT = primary(Path(__file__).resolve().parents[2])
COMPOSE_FILE = ROOT / "deploy" / "compose" / "ledger.yaml"
SERVICE = "ledger"
ADOPTED_SERVICE = "swarm"
TIMEOUT_S = 120
ERROR_KEPT = 200

Run = Callable[..., subprocess.CompletedProcess]


def adopted(folder: Path, environ: dict = os.environ) -> bool:
    recorded = adoption(environ)
    return bool(recorded) and Path(recorded).resolve() == folder.resolve()


def adoption(environ: dict = os.environ) -> str:
    swarm = ports.swarm()
    return swarm.stack_env.read_env(swarm.stack_init.stack_home(environ) / ".env").get("SWARM_ADOPT_LEDGER_DIR", "")


def used(
    folder: Path,
    environ: dict = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    compose_file: Path = COMPOSE_FILE,
) -> bool:
    if environ.get("LEDGER_SERVER_RUNTIME") == "process":
        return False
    shared = ledger_link.shared_directory(environ={"LEDGER_DIR": str(folder)})
    if adoption(environ):
        return shared or adopted(folder, environ)
    return shared and which("docker") is not None and compose_file.is_file()


def _compose(action: list[str], folder: Path, environ: dict, run: Run) -> str:
    if adopted(folder, environ):
        stack_init = ports.swarm().stack_init
        argv, env = stack_init.compose(stack_init.stack_home(environ), *action, ADOPTED_SERVICE), environ
    elif recorded := adoption(environ):
        return f"the swarm stack adopts {recorded}, not {folder}; the legacy ledger service stays down"
    else:
        argv, env = _legacy(action, folder, environ)
    try:
        done = run(argv, env=env, capture_output=True, text=True, check=False, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"docker compose timed out after {TIMEOUT_S} seconds"
    except OSError as exc:
        return str(exc)
    if done.returncode == 0:
        return ""
    return (done.stderr or "").strip()[-ERROR_KEPT:] or f"docker compose exit code {done.returncode}"


def _legacy(action: list[str], folder: Path, environ: dict) -> tuple[list[str], dict]:
    env = {
        **environ,
        "LEDGER_DIR": str(folder),
        "LEDGER_CODE": str(ROOT),
        "LEDGER_UID": str(os.getuid()),
        "LEDGER_GID": str(os.getgid()),
    }
    return ["docker", "compose", "-f", str(COMPOSE_FILE), *action, SERVICE], env


def start(folder: Path, environ: dict = os.environ, run: Run = subprocess.run) -> str:
    return _compose(["up", "-d"], folder, environ, run)


def stop(folder: Path, environ: dict = os.environ, run: Run = subprocess.run) -> str:
    return _compose(["stop"], folder, environ, run)
