import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

TARGETS = ("claude", "codex")
RECORD = "execution.json"
PENDING = ".bootstrap-pending"
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
VARIABLE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
ABSOLUTE = re.compile(r"""(?:^|[\s'"=(;])(/[^\s'";)]*)""")
SYSTEM_ROOTS = (Path("/usr/bin"), Path("/bin"), Path("/usr/local/bin"))


class BootstrapError(ValueError):
    pass


class Request:
    def __init__(
        self,
        root: Path,
        attempt: str,
        templates: Path,
        profiles: dict[str, str],
        interpreter: Path,
        accounts: dict[str, str],
        endpoints: dict[str, str],
        uid: int,
        gid: int,
    ) -> None:
        self.root = root
        self.attempt = attempt
        self.templates = templates
        self.profiles = profiles
        self.interpreter = interpreter
        self.accounts = accounts
        self.endpoints = endpoints
        self.uid = uid
        self.gid = gid

    def document(self) -> dict:
        return {
            "attempt": self.attempt,
            "profiles": self.profiles,
            "interpreter": str(self.interpreter),
            "accounts": self.accounts,
            "endpoints": self.endpoints,
            "uid": self.uid,
            "gid": self.gid,
        }


def _validate(request: Request) -> None:
    if not NAME.fullmatch(request.attempt):
        raise BootstrapError(f"invalid attempt id: {request.attempt}")
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
    for target, variable in request.accounts.items():
        if target not in request.profiles:
            raise BootstrapError(f"account reference for unrequested target: {target}")
        if not VARIABLE.fullmatch(variable):
            raise BootstrapError(f"invalid account reference for {target}")
    for key, url in request.endpoints.items():
        parts = urlsplit(url)
        if (
            not VARIABLE.fullmatch(key)
            or parts.scheme not in ("http", "https")
            or not parts.hostname
            or "@" in parts.netloc
        ):
            raise BootstrapError(f"invalid service endpoint: {key}")
    if (os.geteuid(), os.getegid()) != (request.uid, request.gid):
        raise BootstrapError(f"bootstrap must run as {request.uid}:{request.gid}")


def interpreter_prefix(interpreter: Path) -> Path:
    try:
        done = subprocess.run(
            [str(interpreter), "-c", "import hooks, sys; print(sys.prefix)"],
            cwd="/",
            capture_output=True,
            text=True,
            timeout=30,
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


def _digest(request: Request) -> str:
    digest = hashlib.sha256(json.dumps(request.document(), sort_keys=True).encode())
    for name in sorted(set(request.profiles.values())):
        source = request.templates / name
        for path in sorted(source.rglob("*")):
            digest.update(f"{path.relative_to(source)}\0".encode())
            if path.is_symlink():
                digest.update(os.readlink(path).encode())
            elif path.is_file():
                digest.update(path.read_bytes())
    return digest.hexdigest()


def child_command(attempt: Path, target: str) -> list[str]:
    return [sys.executable, "-m", "scripts.swarm_v2.worker_home", "render", str(attempt), target]


def child_environment(home: Path, interpreter: Path) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "AGENTIHOOKS_HOME": str(home / ".agentihooks"),
        "AGENTIHOOKS_PYTHON": str(interpreter),
        "AGENTIHOOKS_MCP_TRANSPORT": "stdio",
    }
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    return env


def render(attempt: Path, target: str) -> None:
    pending = json.loads((attempt / PENDING).read_text())
    home = attempt / "homes" / target
    with open(attempt / "run" / f"render-{target}.log", "w") as log:
        done = subprocess.run(
            child_command(attempt, target),
            cwd=home,
            env=child_environment(home, Path(pending["request"]["interpreter"])),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if done.returncode:
        raise BootstrapError(f"{target} render failed with exit {done.returncode}")


def materialize(attempt: Path, target: str) -> None:
    from scripts.profiles.render import _is_doc, _settings
    from scripts.targets import get_adapter
    from scripts.targets._common import _install_module

    _i = _install_module()
    request = json.loads((attempt / PENDING).read_text())["request"]
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


def _claude_commands(home: Path) -> dict[str, list[str]]:
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    hooks = [h.get("command", "") for groups in settings.get("hooks", {}).values() for g in groups for h in g["hooks"]]
    servers = json.loads((home / ".claude.json").read_text()).get("mcpServers", {})
    return {
        "hook command": [*hooks, (settings.get("statusLine") or {}).get("command", "")],
        "MCP server": [_server_text(spec) for spec in servers.values()],
    }


def _codex_commands(home: Path) -> dict[str, list[str]]:
    codex = home / ".codex"
    config = tomllib.loads((codex / "config.toml").read_text())
    hooks = json.loads((codex / "hooks.json").read_text())["hooks"]
    wrapper = (codex / "agentihooks-hook.sh").read_text().splitlines()[1:]
    return {
        "hook command": [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]] + wrapper,
        "notify command": [" ".join(config.get("notify", []))],
        "MCP server": [_server_text(spec) for spec in config.get("mcp_servers", {}).values()],
    }


def _server_text(spec: dict) -> str:
    return " ".join([spec.get("command", ""), *spec.get("args", []), spec.get("cwd", "")])


def _check_home(attempt: Path, target: str, roots: list[Path], owner: tuple[int, int]) -> None:
    home = attempt / "homes" / target
    surfaces = _claude_commands(home) if target == "claude" else _codex_commands(home)
    for surface, texts in surfaces.items():
        for text in texts:
            for token in ABSOLUTE.findall(text):
                if _escapes(Path(os.path.normpath(token)), roots):
                    raise BootstrapError(f"{target} {surface} leaves the execution root: {token}")
    for path in [home, *home.rglob("*")]:
        if path.is_symlink() and _escapes(path.resolve(), roots):
            raise BootstrapError(f"{target} link leaves the execution root: {path.resolve()}")
        stat = path.lstat()
        if (stat.st_uid, stat.st_gid) != owner:
            raise BootstrapError(f"{target} home holds a file not owned by {owner[0]}:{owner[1]}")


def _seed(attempt: Path, request: Request) -> None:
    linked = []
    for name in sorted(set(request.profiles.values())):
        copy = attempt / "profiles" / name
        shutil.copytree(request.templates / name, copy, symlinks=True)
        linked.append({"name": name, "path": str(copy)})
    for target in request.profiles:
        state = attempt / "homes" / target / ".agentihooks"
        state.mkdir(parents=True, mode=0o700)
        (state / "state.json").write_text(json.dumps({"linked_profiles": linked}, indent=2) + "\n")


def _accepted(attempt: Path, digest: str) -> dict | None:
    if not attempt.exists():
        return None
    record = attempt / RECORD
    if record.is_file():
        accepted = json.loads(record.read_text())
        if accepted["digest"] != digest:
            raise BootstrapError(f"attempt {attempt.name} was accepted from a different request")
        return {**accepted, "reused": True}
    if not (attempt / PENDING).is_file():
        raise BootstrapError(f"{attempt.name} holds files bootstrap did not write")
    shutil.rmtree(attempt)
    return None


def bootstrap(request: Request) -> dict:
    _validate(request)
    for name in set(request.profiles.values()):
        _check_template(request.templates / name, name)
    roots = [request.root.resolve(), interpreter_prefix(request.interpreter), *code_roots(), *SYSTEM_ROOTS]
    digest = _digest(request)
    attempt = request.root / request.attempt
    accepted = _accepted(attempt, digest)
    if accepted is not None:
        return accepted
    started = time.monotonic()
    attempt.mkdir(mode=0o700)
    try:
        (attempt / PENDING).write_text(json.dumps({"digest": digest, "request": request.document()}))
        for folder in ("run", "tmp"):
            (attempt / folder).mkdir(mode=0o700)
        _seed(attempt, request)
        for target in request.profiles:
            render(attempt, target)
            _check_home(attempt, target, roots, (request.uid, request.gid))
    except BootstrapError:
        shutil.rmtree(attempt)
        raise
    record = {
        "schema_version": 1,
        "package": "SV2-IMG-02",
        "attempt": request.attempt,
        "digest": digest,
        "profiles": request.profiles,
        "accounts": request.accounts,
        "endpoints": request.endpoints,
        "interpreter": str(request.interpreter),
        "homes": {target: f"homes/{target}" for target in request.profiles},
        "worker_profile_materialization_seconds": round(time.monotonic() - started, 3),
    }
    staged = attempt / f"{RECORD}.tmp"
    staged.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    staged.replace(attempt / RECORD)
    (attempt / PENDING).unlink()
    return {**record, "reused": False}


def _pairs(values: list[str], flag: str) -> dict[str, str]:
    pairs = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator:
            raise BootstrapError(f"{flag} needs KEY=VALUE: {value}")
        pairs[key] = item
    return pairs


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
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
        print(json.dumps(bootstrap(request), indent=2, sort_keys=True))
    except BootstrapError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
