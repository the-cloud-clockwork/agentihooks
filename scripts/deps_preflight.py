"""`agentihooks deps check|ensure` — the bundle's dev-environment dependencies, checked and installed before launch.

The manifest is ``<bundle>/deps.json``, read from the root bundle and then the user bundle; an
entry of the user bundle replaces the root entry with the same id. Each entry declares how to check
itself and, unless its kind is ``system``, how to install itself. System dependencies need the
operator (sudo, a package manager), so they are only reported.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from scripts import mcp_daemon, tool_deps

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

KINDS = ("uv-tool", "npm", "pip", "claude-plugin", "system")
STAMP_TTL_SECONDS = 6 * 3600
CHECK_TIMEOUT_SECONDS = 10
INSTALL_TIMEOUT_SECONDS = 600
EXIT_MISSING_SYSTEM = 4


@dataclass(frozen=True)
class Dep:
    id: str
    kind: str
    check: tuple[str, ...]
    install: tuple[str, ...] = ()
    present: bool = True
    required: bool = True
    affects_sessions: bool = False
    hint: str = ""


def parse(manifest: dict) -> list[Dep]:
    deps = []
    for raw in manifest.get("deps", []):
        if raw.get("kind") not in KINDS:
            raise ValueError(f"deps.json: {raw.get('id')!r} has kind {raw.get('kind')!r}; allowed: {', '.join(KINDS)}")
        if raw["kind"] != "system" and not raw.get("install"):
            raise ValueError(f"deps.json: {raw['id']!r} is installable but declares no install command")
        deps.append(
            Dep(
                id=raw["id"],
                kind=raw["kind"],
                check=tuple(raw["check"]),
                install=tuple(raw.get("install", ())),
                present=raw.get("state", "present") == "present",
                required=raw.get("required", True),
                affects_sessions=raw.get("affects_sessions", False),
                hint=raw.get("hint", ""),
            )
        )
    return deps


def manifest_path() -> Path | None:
    from scripts.install import _get_bundle_path

    bundle = _get_bundle_path()
    path = bundle / "deps.json" if bundle else None
    return path if path and path.is_file() else None


def user_manifest_path() -> Path | None:
    from scripts.install import _get_user_bundle_path

    user = _get_user_bundle_path()
    path = user / "deps.json" if user else None
    return path if path and path.is_file() else None


def manifest_paths() -> list[Path]:
    return [path for path in (manifest_path(), user_manifest_path()) if path]


def declared(paths: list[Path] | None = None) -> list[Dep]:
    merged: dict[str, Dep] = {}
    for path in manifest_paths() if paths is None else paths:
        merged.update({dep.id: dep for dep in parse(json.loads(path.read_text()))})
    return list(merged.values())


def fleet_plugins(bundle: Path | None, user: Path | None = None) -> list[str]:
    paths = [root / "deps.json" for root in (bundle, user) if root is not None and (root / "deps.json").is_file()]
    return [dep.id for dep in declared(paths) if dep.kind == "claude-plugin" and dep.present]


def _stamp() -> Path:
    return mcp_daemon.state_dir() / "deps.stamp"


def installs_log() -> Path:
    return mcp_daemon.state_dir() / "deps-installs.jsonl"


def _sha(*paths: Path) -> str:
    digests = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    return digests[0] if len(digests) == 1 else hashlib.sha256("".join(digests).encode()).hexdigest()


def stamp_fresh(*paths: Path) -> bool:
    try:
        stamp = json.loads(_stamp().read_text())
    except (OSError, ValueError):
        return False
    return stamp.get("manifest_sha") == _sha(*paths) and time.time() - stamp.get("ok_at", 0) < STAMP_TTL_SECONDS


def _run(argv: tuple[str, ...], timeout: float) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def satisfied(dep: Dep) -> bool:
    return _run(dep.check, CHECK_TIMEOUT_SECONDS) == dep.present


def _record(dep: Dep, ok: bool) -> None:
    installs_log().parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.time(), "id": dep.id, "kind": dep.kind, "ok": ok, "affects_sessions": dep.affects_sessions}
    with installs_log().open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def _fix(dep: Dep) -> bool:
    ok = _run(dep.install, INSTALL_TIMEOUT_SECONDS) and satisfied(dep)
    _record(dep, ok)
    return ok


@contextlib.contextmanager
def _lock():
    path = mcp_daemon.state_dir() / "deps.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def refresh_codex_wrapper(caller: str) -> None:
    from scripts.targets import codex_target

    try:
        codex_target.refresh_wrapper()
    except Exception as exc:  # noqa: BLE001 - a broken bundle must not stop installs or a launch
        print(f"[agentihooks {caller}] hook wrapper refresh skipped: {exc}", file=sys.stderr)


def install_declared(*, quiet: bool) -> None:
    specs = tool_deps.missing_here()
    if not specs:
        return
    if not quiet:
        print(
            f"[agentihooks] deps: installing {', '.join(specs)} into the agentihooks tool environment", file=sys.stderr
        )
    try:
        tool_deps.install(specs, tool_deps.call, sys.executable)
    except RuntimeError as exc:
        if not quiet:
            print(f"[agentihooks] deps: {exc}", file=sys.stderr)


def ensure(*, quiet: bool = False) -> int:
    from scripts.skill_links import repair

    repair()
    refresh_codex_wrapper("deps")
    paths = manifest_paths()
    if not paths or stamp_fresh(*paths):
        return 0
    with _lock():
        if stamp_fresh(*paths):
            return 0
        install_declared(quiet=quiet)
        missing_system = []
        for dep in declared(paths):
            if satisfied(dep):
                continue
            if dep.kind == "system":
                if dep.required:
                    missing_system.append(dep)
                continue
            action = "removing" if not dep.present else "installing"
            if not quiet:
                print(f"[agentihooks] deps: {action} {dep.id}", file=sys.stderr)
            if not _fix(dep) and not quiet:
                print(f"[agentihooks] deps: {action} {dep.id} failed", file=sys.stderr)
        for dep in missing_system:
            print(f"[agentihooks] deps: {dep.id} is missing and needs the operator: {dep.hint}", file=sys.stderr)
        if missing_system:
            return EXIT_MISSING_SYSTEM
        _stamp().write_text(json.dumps({"manifest_sha": _sha(*paths), "ok_at": time.time()}))
        return 0


def unsatisfied() -> list[Dep]:
    from scripts.skill_links import dangling

    missing = [
        Dep(id=f"skill:{link.name}", kind="skill", check=(), hint=f"dangling link: {link}") for link in dangling()
    ]
    missing.extend(
        Dep(
            id=spec,
            kind="python",
            check=(),
            hint="declared in pyproject.toml but missing from the agentihooks tool environment; install it with "
            f"uv pip install --python {sys.executable} '{spec}'",
        )
        for spec in tool_deps.missing_here()
    )
    missing.extend(dep for dep in declared() if not satisfied(dep))
    return missing


def sync() -> int:
    repo = str(tool_deps.SOURCE)
    try:
        branch = tool_deps.call(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"])
        if branch != "dev":
            print(f"deps sync: {repo} is on {branch}, not dev", file=sys.stderr)
            return 1
        installed = tool_deps.pull(repo, tool_deps.call, sys.executable)
    except RuntimeError as exc:
        print(f"deps sync: {exc}", file=sys.stderr)
        return 1
    if installed:
        print(f"deps sync: installed {', '.join(installed)} into the agentihooks tool environment")
    else:
        print("deps sync: pulled dev; declared dependencies unchanged")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks deps")
    parser.add_argument("action", choices=["check", "ensure", "mark-changed", "sync"])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--reason", default="manual", help="mark-changed: what changed (recorded in the install log)")
    args = parser.parse_args(argv)
    if args.action == "ensure":
        return ensure(quiet=args.quiet)
    if args.action == "sync":
        return sync()
    if args.action == "mark-changed":
        _record(Dep(id=args.reason, kind="system", check=(), affects_sessions=True), ok=True)
        return 0
    missing = unsatisfied()
    for dep in missing:
        print(f"missing: {dep.id} ({dep.kind}){' — ' + dep.hint if dep.hint else ''}")
    print("deps: all satisfied" if not missing else f"deps: {len(missing)} unsatisfied")
    return 1 if missing else 0
