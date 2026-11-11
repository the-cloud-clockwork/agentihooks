"""Import agentihub agent packages as portable bundle profiles.

Each package under ``agents/<agent>/package`` becomes ``profiles/<name>`` in the
bundle: ``command.yml`` turns into ``profile.yml``, instructions, skills and rules
are copied, and Claude settings become overrides with absolute paths removed.
``state/`` and ``reports/`` go to a runtime data folder, Node and pip installs
become bundle ``deps.json`` entries, and evaluation cases become judge cases.
Secret files, symlinks, unsafe names and taken names refuse the whole import
before any file is read for content or written; literal credential values and
guard hooks the import would drop refuse it before any write. Profiles are
staged first and moved into place only once all are written; an error while
moving them removes the ones already moved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TypeGuard

import yaml

from hooks import secrets

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
GUARD_EVENTS = ("PreToolUse", "PermissionRequest", "UserPromptSubmit")
CREDENTIAL_KEY = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|CREDENTIALS?)$", re.IGNORECASE
)
ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
ENV_TEMPLATES = frozenset({"dist", "example", "sample", "template"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ABSOLUTE = re.compile(r"(?:^|[\s(=\"'])(?:/|~/|[A-Za-z]:[\\/]|file:/|\\\\)")
DATA = ("state", "reports")
CASES = "evaluation/cases"
CASE_SUFFIXES = (".yaml", ".yml")
DEPS = "deps.json"
EMPTY_DEPS = '{\n  "deps": [\n  ]\n}\n'
INSTALLS = (("npm", "package.json"), ("pip", "requirements.txt"))
CHECKS = {
    "npm": ("npm", "ls", "--global"),
    "pip": ("python3", "-c", "import importlib.metadata as m, sys; [m.distribution(n) for n in sys.argv[1:]]"),
}
INSTALL_COMMANDS = {"npm": ("npm", "install", "--global"), "pip": ("python3", "-m", "pip", "install")}
REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
LOCAL_VERSIONS = ("file:", "link:", ".")
EMPTY: tuple[object, ...] = (None, "", [], {})
_DROP = object()


def import_hub(src: Path, out: Path, dry_run: bool, data: Path | None = None) -> dict[str, object]:
    """Convert every hub package under ``src`` into a profile under ``out``, its runtime data under ``data``."""
    data = data or Path(os.environ.get("AGENTIHOOKS_HOME", Path.home() / ".agentihooks")) / "hub-data"
    packages = _packages(src)
    for package in packages:
        _refuse_unsafe_files(package)
    plans: dict[str, dict[str, bytes]] = {}
    stores: dict[str, dict[str, bytes]] = {}
    deps: list[dict[str, object]] = []
    for package in packages:
        command = _command(package)
        name = _profile_name(command, package.parent.name)
        if name in plans or (out / "profiles" / name).exists():
            raise ValueError(f"profile name taken: {name}")
        if (data / "profiles" / name).exists():
            raise ValueError(f"profile data taken: {name}")
        plans[name] = _plan(package, name, command) | _cases(package.parent / CASES)
        stores[name] = _collect(package, DATA)
        deps.extend(_deps(package, name))
    manifest = _manifest(out / DEPS, deps)
    if not dry_run:
        _write(out, plans)
        if any(stores.values()):
            _write(data, {name: files for name, files in stores.items() if files})
        if deps:
            _replace(out / DEPS, manifest)
    files = sorted(f"profiles/{name}/{relative}" for name, plan in plans.items() for relative in plan)
    return {
        "profiles": sorted(plans),
        "files": files,
        "data": sorted(
            str(data / "profiles" / name / relative) for name, store in stores.items() for relative in store
        ),
        "deps": [str(dep["id"]) for dep in deps],
        "cases": [path for path in files if path.split("/", 2)[2].startswith("cases/")],
        "dry_run": dry_run,
    }


def _packages(src: Path) -> list[Path]:
    """Return every package directory holding a ``command.yml``, sorted."""
    return sorted(command.parent for command in (src / "agents").glob("*/package/command.yml"))


def _is_secret(name: str) -> bool:
    """Tell whether a file name is on the secret denylist."""
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
        return True
    return name.startswith(".env.") and name.removeprefix(".env.") not in ENV_TEMPLATES


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
    if not _is_mapping(loaded):
        raise ValueError(f"command.yml is not a mapping: {package}")
    return loaded


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
    if _is_mapping(claude):
        kept = {key: claude[key] for key in CLAUDE_KEYS if key in claude}
        if kept:
            profile["claude"] = kept
    return profile


def _portable(value: object) -> object:
    """Drop strings carrying an absolute path, hooks losing their command and containers left empty."""
    if isinstance(value, str):
        return _DROP if ABSOLUTE.search(value) else value
    if _is_mapping(value):
        kept = {key: item for key, item in ((key, _portable(item)) for key, item in value.items()) if item is not _DROP}
        lost_command = "command" in value and "command" not in kept
        return _DROP if lost_command or (value and not kept) else kept
    if _is_list(value):
        kept_items = [item for item in map(_portable, value) if item is not _DROP]
        return _DROP if value and not kept_items else kept_items
    return value


def _is_mapping(value: object) -> TypeGuard[dict[str, object]]:
    """Tell whether a parsed JSON or YAML value is a mapping."""
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[object]]:
    """Tell whether a parsed JSON or YAML value is a list."""
    return isinstance(value, list)


def _plan(package: Path, name: str, command: dict[str, object]) -> dict[str, bytes]:
    """Return each profile file's relative path and content."""
    files = {"profile.yml": yaml.safe_dump(_profile(command, name)).encode()} | _collect(package, COPIED)
    settings_path = package / SETTINGS
    if settings_path.is_file():
        settings: object = json.loads(settings_path.read_text())
        _refuse_lost_restrictions(settings, settings_path)
        _refuse_lost_guards(settings, settings_path)
        _refuse_literal_credentials(settings, settings_path)
        portable = _portable(settings)
        if portable is not _DROP:
            files[OVERRIDES] = (json.dumps(portable, indent=2) + "\n").encode()
    return files


def _collect(package: Path, relatives: tuple[str, ...]) -> dict[str, bytes]:
    """Return each file under the given package paths by its path relative to the package."""
    files: dict[str, bytes] = {}
    for relative in relatives:
        source = package / relative
        paths = [source] if source.is_file() else sorted(source.rglob("*"))
        for path in paths:
            if path.is_file():
                files[path.relative_to(package).as_posix()] = path.read_bytes()
    return files


def _cases(folder: Path) -> dict[str, bytes]:
    """Return a judge case file for each hub evaluation case in ``folder``."""
    files: dict[str, bytes] = {}
    for path in sorted(folder.glob("*")):
        if path.suffix in CASE_SUFFIXES:
            if path.is_symlink():
                raise ValueError(f"symlink refused: {path}")
            files[f"cases/{path.stem}.json"] = (json.dumps(_judge_case(path), indent=2) + "\n").encode()
    return files


def _judge_case(path: Path) -> dict[str, object]:
    """Map a hub evaluation case onto a judge case: its description as intent, each expectation a point."""
    loaded: object = yaml.safe_load(path.read_text())
    if not _is_mapping(loaded):
        raise ValueError(f"eval case is not a mapping: {path}")
    intent = loaded.get("description") or loaded.get("name")
    expected = loaded.get("expected")
    points = _points(expected) if _is_mapping(expected) else []
    if not isinstance(intent, str) or not intent or not points:
        raise ValueError(f"eval case needs an intent and points: {path}")
    case: dict[str, object] = {"intent": intent, "points": points}
    if any(secrets.scan(text, mode="strict") for text in secrets.iter_strings(case)):
        raise ValueError(f"literal credential refused: {path}")
    return case


def _points(expected: dict[str, object]) -> list[str]:
    """Return one point per non-empty expectation item."""
    points: list[str] = []
    for key, value in expected.items():
        for item in value if _is_list(value) else [value]:
            if item not in EMPTY:
                points.append(f"{key}: {item if isinstance(item, str) else json.dumps(item, default=str)}")
    return points


def _deps(package: Path, name: str) -> list[dict[str, object]]:
    """Return a bundle deps entry for each Node and pip manifest in the package that installs something."""
    entries: list[dict[str, object]] = []
    for kind, manifest in INSTALLS:
        for path in sorted(package.rglob(manifest)):
            if "node_modules" in path.relative_to(package).parts:
                continue
            names, specs = _npm(path) if kind == "npm" else _pip(path)
            folder = path.parent.relative_to(package).as_posix()
            if specs:
                entries.append(
                    {
                        "id": f"{name}-{kind}-{'root' if folder == '.' else folder.replace('/', '-')}",
                        "kind": kind,
                        "check": [*CHECKS[kind], *names],
                        "install": [*INSTALL_COMMANDS[kind], *specs],
                    }
                )
    return entries


def _npm(path: Path) -> tuple[list[str], list[str]]:
    """Return the package names and install specs of a ``package.json``'s dependencies, refusing local paths."""
    loaded: object = json.loads(path.read_text())
    dependencies = loaded.get("dependencies") if _is_mapping(loaded) else None
    pairs = sorted(dependencies.items()) if _is_mapping(dependencies) else []
    for _, version in pairs:
        if not isinstance(version, str) or ABSOLUTE.search(version) or version.startswith(LOCAL_VERSIONS):
            raise ValueError(f"install from a local path refused: {path}")
    return [package for package, _ in pairs], [f"{package}@{version}" for package, version in pairs]


def _pip(path: Path) -> tuple[list[str], list[str]]:
    """Return the distribution names and install specs of a ``requirements.txt``, refusing what a name cannot check."""
    names: list[str] = []
    specs: list[str] = []
    for line in path.read_text().splitlines():
        text = line.partition("#")[0].strip().removesuffix("\\").strip()
        tokens = text.split()
        if any(token.startswith("-") and not token.startswith("--hash") for token in tokens):
            raise ValueError(f"requirements option refused: {text} in {path}")
        spec = " ".join(token for token in tokens if not token.startswith("-"))
        if not spec:
            continue
        match = REQUIREMENT.match(spec)
        if not match or "@" in spec or "://" in spec:
            raise ValueError(f"requirement without a checkable name refused: {spec} in {path}")
        names.append(match.group())
        specs.append(spec)
    return names, specs


def _manifest(path: Path, entries: list[dict[str, object]]) -> bytes:
    """Return the bundle deps manifest with ``entries`` appended, keeping existing text, refusing a taken id."""
    text = path.read_text() if path.is_file() else EMPTY_DEPS
    loaded: object = json.loads(text)
    current = loaded.get("deps") if _is_mapping(loaded) else None
    if not _is_mapping(loaded) or not _is_list(current):
        raise ValueError(f"deps.json has no deps list: {path}")
    taken = {dep.get("id") for dep in current if _is_mapping(dep)}
    for entry in entries:
        if entry["id"] in taken:
            raise ValueError(f"deps.json entry taken: {entry['id']}")
        taken.add(entry["id"])
    expected = loaded | {"deps": [*current, *entries]}
    body = text.rstrip().removesuffix("}").rstrip()
    rendered = ",\n".join(
        "    {\n"
        + ",\n".join(f"      {json.dumps(key)}: {json.dumps(value)}" for key, value in entry.items())
        + "\n    }"
        for entry in entries
    )
    appended = f"{body.removesuffix(']').rstrip()}{',' if current else ''}\n{rendered}\n  ]\n}}\n"
    if not body.endswith("]") or not _parses_to(appended, expected):
        appended = json.dumps(expected, indent=2) + "\n"
    return appended.encode()


def _parses_to(text: str, expected: object) -> bool:
    """Tell whether ``text`` is JSON equal to ``expected``."""
    try:
        return json.loads(text) == expected
    except json.JSONDecodeError:
        return False


def _replace(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a sibling file so a reader never sees it half written."""
    staged = path.with_name(f".{path.name}.tmp")
    staged.write_bytes(data)
    staged.replace(path)


def _refuse_lost_restrictions(settings: object, path: Path) -> None:
    """Refuse settings whose deny or ask rules carry an absolute path that import would drop."""
    permissions = settings.get("permissions") if _is_mapping(settings) else None
    if not _is_mapping(permissions):
        return
    for key in RESTRICTIONS:
        rules = permissions.get(key)
        if _is_list(rules) and any(isinstance(rule, str) and ABSOLUTE.search(rule) for rule in rules):
            raise ValueError(f"{key} rule with an absolute path refused: {path}")


def _refuse_lost_guards(settings: object, path: Path) -> None:
    """Refuse settings whose guard hooks carry an absolute path that import would drop."""
    hooks = settings.get("hooks") if _is_mapping(settings) else None
    if not _is_mapping(hooks):
        return
    for event in GUARD_EVENTS:
        if any(ABSOLUTE.search(text) for text in secrets.iter_strings(hooks.get(event))):
            raise ValueError(f"{event} hook with an absolute path refused: {path}")


def _refuse_literal_credentials(settings: object, path: Path) -> None:
    """Refuse settings holding a credential value instead of an environment reference."""
    env = settings.get("env") if _is_mapping(settings) else None
    if _is_mapping(env):
        for key, value in env.items():
            if CREDENTIAL_KEY.search(key) and isinstance(value, str) and value and not ENV_REFERENCE.fullmatch(value):
                raise ValueError(f"literal credential refused, use an environment reference: {key} in {path}")
    if any(secrets.scan(text, mode="strict") for text in secrets.iter_strings(settings)):
        raise ValueError(f"literal credential refused: {path}")


def _write(out: Path, plans: dict[str, dict[str, bytes]]) -> None:
    """Stage every planned profile, then move them into place together, leaving none on failure or naming the one left behind."""
    profiles = out / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=out))
    committed: list[Path] = []
    try:
        for name, files in plans.items():
            for relative, data in files.items():
                target = staging / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        for name in plans:
            target = profiles / name
            if target.exists():
                raise ValueError(f"profile name taken: {name}")
            (staging / name).rename(target)
            committed.append(target)
    except BaseException as exc:
        for target in committed:
            try:
                target.rename(staging / target.name)
            except OSError:
                exc.add_note(f"remove left over profile: {target}")
        raise
    finally:
        shutil.rmtree(staging)


def main(argv: list[str] | None = None) -> int:
    """Import a hub checkout into a bundle checkout and print the report as JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("src")
    parser.add_argument("out")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data")
    args = parser.parse_args(argv)
    data = Path(args.data) if args.data else None
    print(json.dumps(import_hub(Path(args.src), Path(args.out), args.dry_run is True, data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
