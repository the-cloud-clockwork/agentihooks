import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from scripts.profiles import binding, homes
from scripts.routing import envs, native_login


def _configuration(source: Path, home: Path) -> None:
    for entry in source.iterdir():
        if entry.name in (".credentials.json", "auth.json", binding.FILE):
            continue
        target = home / entry.name
        if entry.is_dir():
            if entry.name == "plans":
                shutil.copytree(entry, target, symlinks=True)
            else:
                target.symlink_to(entry.resolve())
        else:
            shutil.copy2(entry, target)


def _binding(agent: str, source: Path, home: Path, environ: Mapping[str, str]) -> None:
    profile = environ.get("AGENTIHOOKS_PROFILE")
    if not profile or not (source / binding.FILE).is_file():
        return
    rendered = binding.inspect(source, profile, agent)
    shutil.copy2(source.parent / f"{agent}.sources.json", home.parent / f"{agent}.sources.json")
    (home / binding.FILE).write_text(json.dumps({**rendered, "home": str(home.resolve())}) + "\n")
    report = environ.get(binding.REPORT)
    if report:
        binding.request(Path(report), profile, agent, home)


def prepare(agent: str, environ: Mapping[str, str]) -> dict[str, str]:
    variable, credential = {"claude": ("CLAUDE_CONFIG_DIR", ".credentials.json"), "codex": ("CODEX_HOME", "auth.json")}[
        agent
    ]
    ordinary = Path(environ.get("HOME") or Path.home()) / f".{agent}"
    login = ordinary / credential
    if not login.is_file():
        raise ValueError("native default login is unavailable")
    source = Path(environ.get(variable) or ordinary)
    base = Path(environ.get("AGENTIHOOKS_HOME") or ordinary.parent / ".agentihooks") / "profiles"
    key = f"native-default-{agent}"
    root = homes.fresh(base, key, {})
    home = root / agent
    home.mkdir(mode=0o700)
    _configuration(source, home)
    shutil.copy2(login, home / credential)
    (home / credential).chmod(0o600)
    _binding(agent, source, home, environ)
    child = envs.codex_subscription_child(envs.claude_interactive_child(environ, agent))
    child.pop("AGENTIHOOKS_ROUTE_ACCOUNT", None)
    child[variable] = str(home)
    homes.collect(base, key)
    return child


def launch_claude(binary: str, args: list[str], environ: Mapping[str, str], report: str) -> None:
    from scripts.claude_quota_balancer import RoutingError
    from scripts.hive.account_slots import execute
    from scripts.install import _claude_command, _write_route_report  # pyright: ignore[reportPrivateUsage]

    try:
        child = prepare("claude", environ)
        native_login.check(child, "default")
    except (OSError, ValueError, native_login.LoginError) as error:
        raise RoutingError(str(error)) from error
    _write_route_report(report, status="routed", account="default", placement="forced")
    print("[agenti] account=default route=default", flush=True)
    execute("claude", "default", child, lambda env: os.execvpe(binary, _claude_command(binary, args), env))
