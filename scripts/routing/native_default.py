import json
import os
import re
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path

import tomlkit
from tomlkit.container import Container
from tomlkit.items import InlineTable, Item, SingleKey, Table

from scripts.profiles import binding, homes
from scripts.routing import envs, native_login
from scripts.targets.codex_hook_trust import rebase_state


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


def _rename_trust_key(key: SingleKey, value: Item, source: str, target: str) -> None:
    original = key.as_string()
    if key.delimiter == "'" and any(char in "'\x7f" or ord(char) < 32 for char in target):
        replacement = original.replace(original.strip(), SingleKey(target + key.key.removeprefix(source)).as_string())
    else:
        encoded_source = source
        encoded_target = target
        if key.delimiter == '"':
            tokens = re.findall(r"\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)|[^\\]", original.strip()[1:-1])
            encoded_source = "".join(tokens[: len(source)])
            encoded_target = SingleKey(target).as_string()[1:-1]
        replacement = original.replace(encoded_source, encoded_target, 1)
    setattr(key, "_original", replacement)
    if isinstance(value, Table) and value.display_name is not None:
        value.display_name = value.display_name.replace(original.strip(), replacement.strip())


def _trust_entries(container: Container, prefix: tuple[str, ...]) -> Iterator[tuple[Container, SingleKey, Item]]:
    for key, value in container.body:
        if key is None:
            continue
        names = (*prefix, *(part.key for part in key))
        if names[:-1] == ("hooks", "state") and isinstance(key, SingleKey):
            yield container, key, value
        elif isinstance(value, (Table, InlineTable)):
            yield from _trust_entries(value.value, names)


def _remove_trust_entries(entries: list[tuple[Container, SingleKey, Item]]) -> None:
    for container, key, _ in entries:
        if key.key in container:
            container.remove(key)


def _codex_trust(source: Path, home: Path) -> None:
    config = home / "config.toml"
    if not config.is_file():
        return
    doc = tomlkit.parse(config.read_bytes().decode())
    entries: dict[str, list[tuple[Container, SingleKey, Item]]] = {}
    for container, key, value in _trust_entries(doc, ()):
        entries.setdefault(key.key, []).append((container, key, value))
    source_hooks = source / "hooks.json"
    target_hooks = home / "hooks.json"
    destinations: set[str] = set()
    for path in dict.fromkeys((source_hooks, source_hooks.resolve())):
        source_prefix = f"{path}:"
        for name, group in rebase_state(entries, path, target_hooks).items():
            if name in destinations:
                _remove_trust_entries(group)
                continue
            destinations.add(name)
            for _, key, value in group:
                target_prefix = name.removesuffix(key.key.removeprefix(source_prefix))
                _rename_trust_key(key, value, source_prefix, target_prefix)
    if destinations:
        config.write_bytes(tomlkit.dumps(doc).encode())


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
    if agent == "codex":
        _codex_trust(source, home)
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
    from scripts.quick.recipe import execute_native

    execute(
        "claude",
        "default",
        child,
        lambda env: execute_native("claude", _claude_command(binary, args), env, execvpe=os.execvpe),
    )
