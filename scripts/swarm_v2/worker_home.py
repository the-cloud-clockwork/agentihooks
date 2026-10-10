import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from scripts.swarm_v2 import filesystem

TARGETS = ("claude", "codex")
RECORD = "execution.json"
PENDING = ".bootstrap-pending"
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
VARIABLE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
SYSTEM_ROOTS = (Path("/usr/bin"), Path("/bin"), Path("/usr/local/bin"))
PROBE_SECONDS = 30
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*")


class BootstrapError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    root: Path
    attempt: str
    templates: Path
    profiles: dict[str, str]
    interpreter: Path
    accounts: dict[str, str]
    endpoints: dict[str, str]
    uid: int
    gid: int


def _document(request: Request) -> dict:
    return {
        "attempt": request.attempt,
        "profiles": request.profiles,
        "interpreter": str(request.interpreter),
        "accounts": request.accounts,
        "endpoints": request.endpoints,
        "uid": request.uid,
        "gid": request.gid,
    }


def _check_profiles(request: Request) -> None:
    if not request.profiles:
        raise BootstrapError("no target profiles requested")
    for target, name in request.profiles.items():
        if target not in TARGETS:
            raise BootstrapError(f"unsupported target: {target}")
        if not NAME.fullmatch(name):
            raise BootstrapError(f"invalid profile name: {name}")
        source = request.templates / name
        if source.is_symlink() or not source.is_dir():
            raise BootstrapError(f"profile template not found: {name}")


def _check_accounts(request: Request) -> None:
    for target, variable in request.accounts.items():
        if target not in request.profiles:
            raise BootstrapError(f"account reference for unrequested target: {target}")
        if not VARIABLE.fullmatch(variable):
            raise BootstrapError(f"invalid account reference for {target}")


def _check_endpoints(request: Request) -> None:
    for key, url in request.endpoints.items():
        parts = urlsplit(url)
        if (
            not VARIABLE.fullmatch(key)
            or parts.scheme not in ("http", "https")
            or not parts.hostname
            or "@" in parts.netloc
        ):
            raise BootstrapError(f"invalid service endpoint: {key}")


def _validate(request: Request) -> None:
    if not NAME.fullmatch(request.attempt):
        raise BootstrapError(f"invalid attempt id: {request.attempt}")
    if request.root.is_symlink() or not request.root.is_dir():
        raise BootstrapError(f"execution root not found: {request.root}")
    _check_profiles(request)
    _check_accounts(request)
    _check_endpoints(request)
    if (os.geteuid(), os.getegid()) != (request.uid, request.gid):
        raise BootstrapError(f"bootstrap must run as {request.uid}:{request.gid}")


def _check_volume(request: Request) -> None:
    if "codex" in request.profiles and os.statvfs(request.root).f_flag & os.ST_NOEXEC:
        raise BootstrapError("execution root is mounted noexec, so the codex hook wrapper cannot run")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _json(document: object) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _environment() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}


def interpreter_prefix(interpreter: Path) -> Path:
    try:
        done = subprocess.run(
            [str(interpreter), "-c", "import hooks, sys; print(sys.prefix)"],
            cwd="/",
            env=_environment(),
            capture_output=True,
            text=True,
            timeout=PROBE_SECONDS,
        )
    except OSError:
        done = None
    if done is None or done.returncode:
        raise BootstrapError(f"interpreter cannot run agentihooks: {interpreter}")
    return Path(done.stdout.strip()).resolve()


def code_roots() -> list[Path]:
    from scripts.install import install_root

    return [Path(__file__).resolve().parents[2], install_root().resolve()]


def _escapes(path: Path, roots: list[Path]) -> bool:
    return not any(path.is_relative_to(root) for root in roots)


def _check_template(source: Path, name: str) -> None:
    resolved = source.resolve()
    for path in source.rglob("*"):
        if path.is_symlink() and _escapes(path.resolve(), [resolved]):
            raise BootstrapError(f"profile escapes its template: {name}")


def _tree_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        digest.update(f"{path.relative_to(source)}\0".encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _profile_digests(request: Request) -> dict[str, str]:
    return {name: _tree_digest(request.templates / name) for name in sorted(set(request.profiles.values()))}


def _digest(request: Request, profiles: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps([_document(request), profiles], sort_keys=True).encode()).hexdigest()


def child_command(attempt: Path, target: str) -> list[str]:
    return [sys.executable, "-m", "scripts.swarm_v2.worker_home", "render", str(attempt), target]


def child_environment(home: Path, interpreter: Path) -> dict[str, str]:
    return {
        **_environment(),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "HOME": str(home),
        "AGENTIHOOKS_HOME": str(home / ".agentihooks"),
        "AGENTIHOOKS_PYTHON": str(interpreter),
        "AGENTIHOOKS_MCP_TRANSPORT": "stdio",
    }


def render(attempt: Path, target: str) -> None:
    pending = json.loads(_text(attempt / PENDING))
    home = attempt / "homes" / target
    done = subprocess.run(
        child_command(attempt, target),
        cwd=home,
        env=child_environment(home, Path(pending["request"]["interpreter"])),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _write(attempt / "run" / f"render-{target}.log", done.stdout)
    if done.returncode:
        raise BootstrapError(f"{target} render failed with exit {done.returncode}")


def materialize(attempt: Path, target: str) -> None:
    from scripts.profiles.render import _is_doc, _settings
    from scripts.targets import get_adapter
    from scripts.targets._common import _install_module

    _i = _install_module()
    request = json.loads(_text(attempt / PENDING))["request"]
    name = request["profiles"][target]
    dirs = _i._resolve_profile_chain(name)
    if not dirs or dirs[0][0] != name:
        raise BootstrapError(f"profile did not resolve in the execution scope: {name}")
    adapter = get_adapter(target)
    native = _settings(target, None, dirs)
    native.setdefault("_agentihooks", {}).setdefault("env", {}).update(request["endpoints"])
    adapter.write_settings(native)
    for subdir, keep in (
        ("skills", _i._skill_dir_filter()),
        ("agents", _is_doc),
        ("commands", _is_doc),
        ("rules", _is_doc),
    ):
        layers = [(subdir, _i.PACKAGE_FEATURES_DIR / subdir)]
        layers += [(n, p / ".claude" / subdir) for n, p in dirs if (p / ".claude" / subdir).is_dir()]
        adapter.install_features(subdir, layers, keep)
    adapter.install_persona(dirs, [n for n, _ in dirs], None)
    adapter.register_hooks_utils(name)
    for _, path in dirs:
        layer = _i._native_layer_path(path, target, _i._NATIVE_MCP_NAME) or _i._native_layer_path(
            path, "claude", _i._NATIVE_MCP_NAME
        )
        if layer:
            adapter.register_mcp(_i._load_native_layer(layer).get("mcpServers") or {})


def _strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return [value] if isinstance(value, str) else []


def _claude_surfaces(home: Path) -> dict[str, list[str]]:
    settings = json.loads(_text(home / ".claude" / "settings.json"))
    servers = json.loads(_text(home / ".claude.json")).get("mcpServers")
    surfaces = {f"setting {key}": _strings(value) for key, value in settings.items() if key != "permissions"}
    return surfaces | {"MCP server": _strings(servers)}


def _wrapper(text: str) -> tuple[list[str], list[str]]:
    exports, commands = [], []
    for line in text.splitlines()[1:]:
        name, _, value = line.removeprefix("export ").partition("=")
        prefix = f'"${{{name}:='
        if line.startswith("export ") and value.startswith(prefix) and value.endswith('}"'):
            exports += shlex.split(value[len(prefix) : -2])
        else:
            commands.append(line)
    return exports, commands


def _codex_surfaces(home: Path) -> dict[str, list[str]]:
    codex = home / ".codex"
    config = tomllib.loads(_text(codex / "config.toml"))
    hooks = json.loads(_text(codex / "hooks.json"))["hooks"]
    exports, commands = _wrapper(_text(codex / "agentihooks-hook.sh"))
    surfaces = {f"setting {key}": _strings(value) for key, value in config.items() if key != "mcp_servers"}
    return surfaces | {
        "MCP server": _strings(config.get("mcp_servers")),
        "hook command": _strings(hooks) + commands,
        "environment value": exports,
    }


def _pieces(word: str) -> list[str]:
    pieces = []
    for part in [p for item in word.split("=") for p in re.split(r":(?!//)", item)]:
        scheme, separator, rest = part.partition("://")
        if not separator:
            pieces.append(part)
        elif not SCHEME.fullmatch(scheme):
            pieces += [scheme, "/" + rest.lstrip("/")]
        elif scheme.lower().split("+")[-1] == "file":
            pieces.append(urlsplit(part).path)
    return pieces


def _paths(text: str) -> list[str]:
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    pieces = [piece for word in words for piece in _pieces(word)]
    return [piece for piece in pieces if "/" in piece or "$" in piece or piece.startswith("~")]


def _leaves(path: str, roots: list[Path]) -> bool:
    if "$" in path or path.startswith("~"):
        return True
    if path.startswith("/"):
        return _escapes(Path(os.path.normpath(path)), roots)
    return ".." in Path(path).parts


def _check_home(attempt: Path, target: str, roots: list[Path], owner: tuple[int, int]) -> None:
    home = attempt / "homes" / target
    surfaces = _claude_surfaces(home) if target == "claude" else _codex_surfaces(home)
    for surface, texts in surfaces.items():
        for path in [p for text in texts for p in _paths(text)]:
            if _leaves(path, roots):
                raise BootstrapError(f"{target} {surface} leaves the execution root: {path}")
    for path in [home, *home.rglob("*")]:
        if path.is_symlink() and _escapes(path.resolve(), roots):
            raise BootstrapError(f"{target} link leaves the execution root: {path.resolve()}")
        stat = path.lstat()
        if (stat.st_uid, stat.st_gid) != owner:
            raise BootstrapError(f"{target} home holds a file not owned by {owner[0]}:{owner[1]}")


def _seed(execution: filesystem.Execution, request: Request) -> None:
    linked = []
    for name in sorted(set(request.profiles.values())):
        copy = filesystem.seed(execution, request.templates / name, name)
        linked.append({"name": name, "path": str(copy)})
    for target in request.profiles:
        state = execution.path("home") / target / ".agentihooks"
        state.mkdir(parents=True, mode=0o700)
        _write(state / "state.json", _json({"linked_profiles": linked}))


def _accepted(attempt: Path, digest: str) -> dict | None:
    if not attempt.exists():
        return None
    record = attempt / RECORD
    if record.is_file():
        accepted = json.loads(_text(record))
        if accepted["digest"] != digest:
            raise BootstrapError(f"attempt {attempt.name} was accepted from a different request")
        return {**accepted, "reused": True}
    if not (attempt / PENDING).is_file():
        raise BootstrapError(f"{attempt.name} holds files bootstrap did not write")
    filesystem.remove(attempt)
    return None


def _record(request: Request, digest: str, profiles: dict[str, str], seconds: float) -> dict:
    return {
        "schema_version": 1,
        "package": "SV2-IMG-02",
        "attempt": request.attempt,
        "digest": digest,
        "profiles": request.profiles,
        "profile_digests": profiles,
        "accounts": request.accounts,
        "endpoints": request.endpoints,
        "interpreter": str(request.interpreter),
        "homes": {target: f"homes/{target}" for target in request.profiles},
        "worker_profile_materialization_seconds": round(seconds, 3),
    }


def _materialize_attempt(request: Request, profiles: dict[str, str], roots: list[Path]) -> dict:
    digest = _digest(request, profiles)
    attempt = request.root / request.attempt
    accepted = _accepted(attempt, digest)
    if accepted is not None:
        return accepted
    started = time.monotonic()
    attempt.mkdir(mode=0o700)
    layout = filesystem.load()
    try:
        _write(attempt / PENDING, _json({"request": _document(request)}))
        execution = filesystem.allocate(request.root, request.attempt, layout)
        _seed(execution, request)
        for target in request.profiles:
            render(attempt, target)
            _check_home(attempt, target, roots, (request.uid, request.gid))
    except (BootstrapError, filesystem.LayoutError) as error:
        filesystem.remove(attempt)
        raise BootstrapError(str(error)) from error
    record = _record(request, digest, profiles, time.monotonic() - started) | {"layout": filesystem.mapping(layout)}
    staged = attempt / f"{RECORD}.tmp"
    _write(staged, _json(record))
    staged.replace(attempt / RECORD)
    (attempt / PENDING).unlink()
    return {**record, "reused": False}


def bootstrap(request: Request) -> dict:
    _validate(request)
    _check_volume(request)
    for name in set(request.profiles.values()):
        _check_template(request.templates / name, name)
    roots = [request.root.resolve(), interpreter_prefix(request.interpreter), *code_roots(), *SYSTEM_ROOTS]
    profiles = _profile_digests(request)
    lock = os.open(request.root, os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _materialize_attempt(request, profiles, roots)
    finally:
        os.close(lock)


def _pairs(values: list[str], flag: str) -> dict[str, str]:
    pairs = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator:
            raise BootstrapError(f"{flag} needs KEY=VALUE: {value}")
        pairs[key] = item
    return pairs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.worker_home")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("bootstrap")
    start.add_argument("--root", type=Path, default=Path("/home/worker/attempts"))
    start.add_argument("--attempt", required=True)
    start.add_argument("--templates", type=Path, default=Path("/opt/agentihooks/templates"))
    start.add_argument("--interpreter", type=Path, default=Path("/opt/venv/bin/python"))
    start.add_argument("--uid", type=int, default=10001)
    start.add_argument("--gid", type=int, default=10001)
    start.add_argument("--profile", action="append", default=[])
    start.add_argument("--account", action="append", default=[])
    start.add_argument("--endpoint", action="append", default=[])
    child = commands.add_parser("render")
    child.add_argument("attempt", type=Path)
    child.add_argument("target", choices=TARGETS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "render":
            materialize(args.attempt, args.target)
            return 0
        request = Request(
            root=args.root,
            attempt=args.attempt,
            templates=args.templates,
            profiles=_pairs(args.profile, "--profile"),
            interpreter=args.interpreter,
            accounts=_pairs(args.account, "--account"),
            endpoints=_pairs(args.endpoint, "--endpoint"),
            uid=args.uid,
            gid=args.gid,
        )
        print(_json(bootstrap(request)), end="")
    except BootstrapError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
