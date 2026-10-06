import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import tomlkit

INSTALL_COMMAND = "curl -fsSL https://herdr.dev/install.sh | sh"
INTEGRATIONS = ("claude", "codex")


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


def turn_off_copy_on_select(path: Path) -> bool:
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    ui = doc.get("ui")
    if ui is None:
        ui = doc["ui"] = tomlkit.table()
    if "copy_on_select" in ui:
        return False
    ui["copy_on_select"] = False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return True


def configure() -> int:
    exe = binary()
    if exe is None:
        print("[herdr] not installed; run: agentihooks herdr install", file=sys.stderr)
        return 1
    before = integration_status()
    failed = [name for name in INTEGRATIONS if subprocess.run([exe, "integration", "install", name]).returncode != 0]
    changed = [name for name in INTEGRATIONS if before.get(name) != "current" and name not in failed]
    if changed:
        from scripts.deps_preflight import main as deps_main

        deps_main(["mark-changed", "--reason", f"herdr-integration:{','.join(changed)}"])
    after = integration_status()
    print("[herdr] integrations: " + " ".join(f"{name}={after.get(name, '?')}" for name in INTEGRATIONS))
    path = config_path()
    if turn_off_copy_on_select(path):
        subprocess.run([exe, "server", "reload-config"], capture_output=True)
        print(f"[herdr] config: copy_on_select = false in {path}")
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
