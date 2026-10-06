import json
import os
import tempfile
import time
from pathlib import Path

WAIT_NOTICE = "the session will wait at Claude's folder trust question until someone answers it in its pane"


def _config_path(environ: dict[str, str]) -> Path:
    from scripts.claude_config import claude_json

    return claude_json(environ)


def allowed(environ: dict[str, str]) -> bool:
    return environ.get("AGENTIHOOKS_TRUST_LAUNCH_DIR", "1").strip().lower() not in ("0", "false", "no", "off")


def _read(config: Path) -> dict:
    if not config.exists():
        return {}
    data = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{config} does not hold a JSON object")
    return data


def _trusted(data: dict, directory: Path) -> bool:
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    return any(
        isinstance(projects.get(str(path)), dict) and projects[str(path)].get("hasTrustDialogAccepted") is True
        for path in (directory, *directory.parents)
    )


def _write(config: Path, data: dict) -> None:
    fd, temp = tempfile.mkstemp(dir=config.parent, prefix=".claude.json.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        if config.exists():
            os.chmod(temp, config.stat().st_mode & 0o777)
        os.replace(temp, config)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _mark(config: Path, directory: Path, lock_timeout: float) -> bool:
    # Claude Code guards this file with a proper-lockfile lock directory beside it.
    lock = config.with_name(config.name + ".lock")
    deadline = time.monotonic() + lock_timeout
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError(f"{lock} is held") from None
            time.sleep(0.1)
    try:
        data = _read(config)
        if _trusted(data, directory):
            return False
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise ValueError(f"{config} projects is not a JSON object")
        entry = projects.setdefault(str(directory), {})
        if not isinstance(entry, dict):
            raise ValueError(f"{config} entry for {directory} is not a JSON object")
        entry["hasTrustDialogAccepted"] = True
        _write(config, data)
        return True
    finally:
        lock.rmdir()


def ensure_trusted(directory: Path, environ: dict[str, str], lock_timeout: float = 3.0) -> tuple[str, str]:
    """('trusted' | 'marked' | 'untrusted', reason when untrusted) for a Claude launch into directory."""
    config = _config_path(environ)
    try:
        if _trusted(_read(config), directory):
            return "trusted", ""
        if not allowed(environ):
            return "untrusted", "AGENTIHOOKS_TRUST_LAUNCH_DIR is off"
        return ("marked" if _mark(config, directory, lock_timeout) else "trusted"), ""
    except (OSError, ValueError) as exc:
        return "untrusted", str(exc)
