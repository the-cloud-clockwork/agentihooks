"""Import agentihub agent packages as portable bundle profiles.

Each package under ``agents/<agent>/package`` becomes ``profiles/<name>`` in the
bundle: ``command.yml`` turns into ``profile.yml``, instructions, skills and rules
are copied, and Claude settings become overrides with absolute paths removed.
Secret files, symlinks, unsafe names and taken names refuse the whole import
before any file is read for content or written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import yaml

PROFILE_KEYS = ("description", "capabilities")
CLAUDE_KEYS = ("model", "effort")
COPIED = ("CLAUDE.md", ".claude/skills", ".claude/rules")
SETTINGS = ".claude/settings.json"
OVERRIDES = ".claude/settings.overrides.json"
SECRET_NAMES = frozenset(
    {
        ".env",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pgpass",
        ".pypirc",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
SECRET_SUFFIXES = (".env", ".key", ".p12", ".pem", ".pfx")
RESTRICTIONS = ("deny", "ask")
ENV_TEMPLATES = frozenset({"dist", "example", "sample", "template"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ABSOLUTE = re.compile(r"(?:^|[\s(=\"'])(?:/|~/|[A-Za-z]:[\\/]|file://)")
_DROP = object()


def import_hub(src: Path, out: Path, dry_run: bool) -> dict[str, object]:
    """Convert every hub package under ``src`` into a profile under ``out``."""
    packages = _packages(src)
    for package in packages:
        _refuse_unsafe_files(package)
    plans: dict[str, dict[str, bytes]] = {}
    for package in packages:
        command = _command(package)
        name = _profile_name(command, package.parent.name)
        if name in plans or (out / "profiles" / name).exists():
            raise ValueError(f"profile name taken: {name}")
        plans[name] = _plan(package, name, command)
    if not dry_run:
        _write(out, plans)
    files = sorted(f"profiles/{name}/{relative}" for name, plan in plans.items() for relative in plan)
    return {"profiles": sorted(plans), "files": files, "dry_run": dry_run}


def _packages(src: Path) -> list[Path]:
    """Return every package directory holding a ``command.yml``, sorted."""
    return sorted(command.parent for command in (src / "agents").glob("*/package/command.yml"))


def _is_secret(name: str) -> bool:
    """Tell whether a file name is on the secret denylist."""
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
        return True
    return name.startswith(".env.") and name.rsplit(".", 1)[-1] not in ENV_TEMPLATES


def _refuse_unsafe_files(package: Path) -> None:
    """Refuse a package holding a symlink or a secret file, reading no content."""
    for path in (package.parent, package):
        if path.is_symlink():
            raise ValueError(f"symlink refused: {path}")
    for root, dirs, files in package.walk():
        for entry in [*dirs, *files]:
            path = root / entry
            if path.is_symlink():
                raise ValueError(f"symlink refused: {path}")
            if entry in files and _is_secret(entry):
                raise ValueError(f"secret file refused: {path}")


def _command(package: Path) -> dict[str, object]:
    """Load a package's ``command.yml`` as a mapping."""
    loaded: object = yaml.safe_load((package / "command.yml").read_text()) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"command.yml is not a mapping: {package}")
    return cast(dict[str, object], loaded)


def _profile_name(command: dict[str, object], default: str) -> str:
    """Return the profile name, refusing one that is not a plain folder name."""
    name = command.get("name", default)
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"unsafe profile name: {name!r}")
    return name


def _profile(command: dict[str, object], name: str) -> dict[str, object]:
    """Map hub command metadata onto the bundle profile schema."""
    profile: dict[str, object] = {"name": name}
    profile.update({key: command[key] for key in PROFILE_KEYS if key in command})
    claude = command.get("claude")
    if isinstance(claude, dict):
        source = cast(dict[str, object], claude)
        kept = {key: source[key] for key in CLAUDE_KEYS if key in source}
        if kept:
            profile["claude"] = kept
    return profile


def _portable(value: object) -> object:
    """Drop strings carrying an absolute path, hooks losing their command and containers left empty."""
    if isinstance(value, str):
        return _DROP if ABSOLUTE.search(value) else value
    if isinstance(value, dict):
        source = cast(dict[str, object], value)
        kept = {
            key: item for key, item in ((key, _portable(item)) for key, item in source.items()) if item is not _DROP
        }
        lost_command = "command" in source and "command" not in kept
        return _DROP if lost_command or (source and not kept) else kept
    if isinstance(value, list):
        items = cast(list[object], value)
        kept_items = [item for item in map(_portable, items) if item is not _DROP]
        return _DROP if items and not kept_items else kept_items
    return value


def _plan(package: Path, name: str, command: dict[str, object]) -> dict[str, bytes]:
    """Return each profile file's relative path and content."""
    files = {"profile.yml": yaml.safe_dump(_profile(command, name), sort_keys=False).encode()}
    for relative in COPIED:
        source = package / relative
        paths = [source] if source.is_file() else sorted(source.rglob("*"))
        for path in paths:
            if path.is_file():
                files[path.relative_to(package).as_posix()] = path.read_bytes()
    settings_path = package / SETTINGS
    if settings_path.is_file():
        settings: object = json.loads(settings_path.read_text())
        _refuse_lost_restrictions(settings, settings_path)
        portable = _portable(settings)
        if portable is not _DROP:
            files[OVERRIDES] = (json.dumps(portable, indent=2) + "\n").encode()
    return files


def _refuse_lost_restrictions(settings: object, path: Path) -> None:
    """Refuse settings whose deny or ask rules carry an absolute path that import would drop."""
    permissions = cast(dict[str, object], settings).get("permissions") if isinstance(settings, dict) else None
    if not isinstance(permissions, dict):
        return
    for key in RESTRICTIONS:
        rules = cast(dict[str, object], permissions).get(key)
        if isinstance(rules, list) and any(
            isinstance(rule, str) and ABSOLUTE.search(rule) for rule in cast(list[object], rules)
        ):
            raise ValueError(f"{key} rule with an absolute path refused: {path}")


def _write(out: Path, plans: dict[str, dict[str, bytes]]) -> None:
    """Write every planned profile, refusing one whose folder appeared since planning."""
    for name, files in plans.items():
        base = out / "profiles" / name
        base.mkdir(parents=True, exist_ok=False)
        for relative, data in files.items():
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
