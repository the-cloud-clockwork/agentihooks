import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import tomlkit
from tomlkit import TOMLDocument
from tomlkit.items import Table

INSTALL_COMMAND = "curl -fsSL https://herdr.dev/install.sh | sh"
INTEGRATIONS = ("claude", "codex")
RESUME_ENV = "AGENTIHOOKS_HERDR_RESUME"


def _install_module():
    mod = sys.modules.get("install") or sys.modules.get("scripts.install")
    if mod is None:
        from scripts import install as mod
    return mod


def binary() -> str | None:
    return shutil.which("herdr")


def choice() -> bool | None:
    herdr = _install_module()._load_state().get("herdr")
    return herdr.get("enabled") if isinstance(herdr, dict) else None


def record(enabled: bool) -> None:
    _i = _install_module()
    state = _i._load_state()
    state["herdr"] = {"enabled": enabled, "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _i._save_state(state)


def integration_status() -> dict[str, str]:
    exe = binary()
    if exe is None:
        return {}
    done = subprocess.run([exe, "integration", "status"], capture_output=True, text=True)
    rows = (line.split(":", 1) for line in done.stdout.splitlines() if ":" in line)
    return {name.strip(): rest.strip().split(" ")[0] for name, rest in rows}


def integration_files() -> dict[str, bytes | None]:
    exe = binary()
    if exe is None:
        return {}
    done = subprocess.run([exe, "integration", "status"], capture_output=True, text=True)
    files: dict[str, bytes | None] = {}
    for line in done.stdout.splitlines():
        name, _, rest = line.partition(":")
        rest = rest.strip()
        if rest.endswith(")") and "(" in rest:
            path = Path(rest[rest.rindex("(") + 1 : -1])
            files[name.strip()] = path.read_bytes() if path.is_file() else None
    return files


def install() -> int:
    print(f"[herdr] installing: {INSTALL_COMMAND}")
    rc = subprocess.run(["sh", "-c", INSTALL_COMMAND]).returncode
    if rc == 0 and binary() is None:
        print("[herdr] installed, but herdr is not on PATH; add ~/.local/bin to PATH", file=sys.stderr)
        return 1
    return rc


def config_path() -> Path:
    explicit = os.environ.get("HERDR_CONFIG_PATH")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr" / "config.toml"


def _table(doc: TOMLDocument, name: str) -> Table:
    table = doc.get(name)
    if table is None:
        table = doc[name] = tomlkit.table()
    return cast("Table", table)


def resume_off(environ: Mapping[str, str] | None = None) -> bool:
    if environ is None:
        from scripts import mcp_daemon

        scan: Callable[[str], str | None] = getattr(mcp_daemon, "_scan_env_file")
        value = os.environ.get(RESUME_ENV) or scan(RESUME_ENV) or ""
    else:
        value = environ.get(RESUME_ENV, "")
    return value.strip().lower() in ("off", "false", "0", "no")


def _herdr_state() -> dict[str, object]:
    state: dict[str, object] = getattr(_install_module(), "_load_state")()
    herdr = state.get("herdr")
    return cast("dict[str, object]", herdr) if isinstance(herdr, dict) else {}


def _released() -> bool:
    return bool(_herdr_state().get("resume_released"))


def _mark_released() -> None:
    install = _install_module()
    state: dict[str, object] = getattr(install, "_load_state")()
    released = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["herdr"] = {**_herdr_state(), "resume_released": released}
    getattr(install, "_save_state")(state)


def _resume(doc: TOMLDocument, changed: list[str]) -> None:
    session = doc.get("session")
    if resume_off():
        if not isinstance(session, Table) or session.get("resume_agents_on_restore") is not False:
            _table(doc, "session")["resume_agents_on_restore"] = False
            changed.append("resume_agents_on_restore = false")
        return
    if _released():
        return
    _mark_released()
    if isinstance(session, Table) and session.unwrap() == {"resume_agents_on_restore": False}:
        del doc["session"]
        changed.append("resume_agents_on_restore restored to the herdr default")


def apply_config(path: Path) -> list[str]:
    doc = tomlkit.parse(path.read_bytes().decode()) if path.exists() else tomlkit.document()
    changed: list[str] = []
    ui = _table(doc, "ui")
    if "copy_on_select" not in ui:
        ui["copy_on_select"] = False
        changed.append("copy_on_select = false")
    _resume(doc, changed)
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((tomlkit.dumps(doc).rstrip("\n") + "\n").encode())
    return changed


def configure() -> int:
    exe = binary()
    if exe is None:
        print("[herdr] not installed; run: agentihooks herdr install", file=sys.stderr)
        return 1
    before = integration_files()
    failed = [name for name in INTEGRATIONS if subprocess.run([exe, "integration", "install", name]).returncode != 0]
    from scripts.profiles.render import operator_codex_home
    from scripts.targets.codex_target import trust_hooks

    trust_hooks(operator_codex_home())
    installed = integration_files()
    changed = [name for name in INTEGRATIONS if installed.get(name) != before.get(name) and name not in failed]
    if changed:
        from scripts.deps_preflight import main as deps_main

        deps_main(["mark-changed", "--reason", f"herdr-integration:{','.join(changed)}"])
    after = integration_status()
    print("[herdr] integrations: " + " ".join(f"{name}={after.get(name, '?')}" for name in INTEGRATIONS))
    path = config_path()
    changed = apply_config(path)
    if changed:
        subprocess.run([exe, "server", "reload-config"], capture_output=True)
        print(f"[herdr] config: {', '.join(changed)} in {path}")
    return 1 if failed else 0


def init_step(flag: str, interactive: bool) -> None:
    """Ask once whether agentihooks should use herdr, remember it, then install or configure it."""
    if flag in ("yes", "no"):
        record(flag == "yes")
    elif choice() is None and interactive:
        answer = input("Install herdr, the terminal runtime agentihooks opens agents in? [Y/n] ").strip().lower()
        record(answer not in ("n", "no"))
    if choice() is not True:
        return
    if binary() is None and install() != 0:
        return
    configure()


def status() -> int:
    exe = binary()
    decided = choice()
    print(f"binary={exe or 'missing'}")
    print(f"enabled={'unset' if decided is None else str(decided).lower()}")
    for name, state in integration_status().items():
        if name in INTEGRATIONS:
            print(f"integration.{name}={state}")
    return 0 if exe else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks herdr", description="Install and configure herdr")
    parser.add_argument("action", nargs="?", choices=("status", "install", "configure", "enable", "disable"))
    args = parser.parse_args(argv)
    if args.action is None:
        if binary() is None and sys.stdin.isatty():
            answer = input("herdr is not installed. Install it now? [Y/n] ").strip().lower()
            init_step("no" if answer in ("n", "no") else "yes", interactive=False)
        return status()
    if args.action == "disable":
        record(False)
        return status()
    if args.action == "status":
        return status()
    record(True)
    if args.action == "install" or binary() is None:
        rc = install()
        if rc != 0:
            return rc
    return configure()
