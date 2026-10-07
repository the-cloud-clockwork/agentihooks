"""Keep swarm development off the operator's live homes, vault, Redis keys and deployment (SV2-FND-05).

Imported by the root conftest before any test module, so the live roots are read from the environment the run
started with, before any fixture redirects it.
"""

import os
import re
import shlex
import shutil
import sys
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

if TYPE_CHECKING:
    from redis import Redis

METRIC = "test_live_path_rejections_total"
MARKER = ".fixture-run"
HOME_NAMES = (
    ".claude",
    ".claude.json",
    ".codex",
    ".copilot",
    ".agentihooks",
    ".agents",
    ".agentibrain",
    ".kube",
    ".config/herdr",
    ".bashrc",
    ".local/bin",
    ".config/systemd/user",
)
CONFIGURED = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_HOME_DIR",
    "AGENTIHOOKS_CLAUDE_HOME",
    "CODEX_HOME",
    "COPILOT_HOME",
    "AGENTIHOOKS_HOME",
    "AGENTIBRAIN_HOME",
    "VAULT_ROOT",
    "KUBECONFIG",
    "HERDR_CONFIG_PATH",
)
LIVE_PROGRAMS = frozenset({"kubectl", "helm", "argocd", "herdr"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
WRAPPERS = frozenset({"env", "timeout", "nohup", "nice", "sudo", "exec", "command", "xargs", "stdbuf", "setsid"})
SHELL_COMMAND_FLAG = re.compile(r"-[a-zA-Z]*c[a-zA-Z]*")
# Every word of a shell command is checked, so a live program named as an argument there is refused too.
TOKENS = re.compile(r"[\s;&|()`{}!<>\"'\\$=]+|:-?")
# os.spawn* raises no audit event, so a program started through it is not seen.
SPAWN_EVENTS = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system"})

REJECTIONS: Counter = Counter()
FIXTURE_ROOT: Path | None = None


def refuse(dimension: str, message: str) -> NoReturn:
    REJECTIONS[dimension] += 1
    pytest.fail(message)


def _expand(part: str) -> Path | None:
    try:
        return Path(part).expanduser()
    except RuntimeError:
        return None


def live_roots(environ: Mapping[str, str], home: Path) -> tuple[Path, ...]:
    roots = [home / name for name in HOME_NAMES]
    if environ.get("XDG_CONFIG_HOME"):
        roots.append(Path(environ["XDG_CONFIG_HOME"]) / "herdr")
    for name in CONFIGURED:
        parts = re.split(f"[,{os.pathsep}]", environ.get(name, ""))
        roots += [path for path in map(_expand, parts) if path is not None and path.is_absolute()]
    return tuple(dict.fromkeys(root.resolve() for root in roots))


REAL_HOME = Path.home()
LIVE_ROOTS = live_roots(os.environ, REAL_HOME)
RUN_ID = uuid.uuid4().hex[:12]
RUN_PREFIX = f"agentihooks-test-{RUN_ID}"


def confine(root: Path, label: str, path: str | os.PathLike) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        refuse("escape", f"{label} resolves outside the fixture directory {root}: {resolved}")
    return resolved


@dataclass(frozen=True)
class Identities:
    run_id: str
    root: Path
    claude_home: Path
    codex_home: Path
    brain_home: Path
    vault: Path
    archive: Path
    kubeconfig: Path
    kube_namespace: str
    redis_url: str
    redis_prefix: str
    traps: dict[str, Path] = field(default_factory=dict)

    def environ(self) -> dict[str, str]:
        return {
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "CODEX_HOME": str(self.codex_home),
            "AGENTIBRAIN_HOME": str(self.brain_home),
            "VAULT_ROOT": str(self.vault),
            "KUBECONFIG": str(self.kubeconfig),
            "AGENTIHOOKS_SWARM_REDIS_URL": self.redis_url,
            "REDIS_KEY_PREFIX": self.redis_prefix,
        }

    def key(self, *parts: str) -> str:
        return ":".join((self.redis_prefix, *parts))


KUBECONFIG = """apiVersion: v1
kind: Config
clusters:
- name: {name}
  cluster:
    server: https://127.0.0.1:9
contexts:
- name: {name}
  context:
    cluster: {name}
    namespace: {name}
current-context: {name}
"""


def build(root: Path, run_id: str | None = None) -> Identities:
    run_id = run_id or uuid.uuid4().hex[:12]
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text(run_id)
    name = f"agentihooks-test-{run_id}"
    homes = {
        "claude_home": root / "claude",
        "codex_home": root / "codex",
        "brain_home": root / "brain",
        "vault": root / "brain" / "vault",
        "archive": root / "archive",
        "kubeconfig": root / "kube" / "config",
    }
    for label, path in homes.items():
        confine(root, label, path)
        (path.parent if label == "kubeconfig" else path).mkdir(parents=True, exist_ok=True)
    homes["kubeconfig"].write_text(KUBECONFIG.format(name=name))
    traps = root / "traps"
    traps.mkdir(exist_ok=True)
    (traps / "live-claude").symlink_to(REAL_HOME / ".claude")
    return Identities(
        run_id=run_id,
        root=root,
        kube_namespace=name,
        redis_url=f"unix://{root / 'redis.sock'}?db=15",
        redis_prefix=name,
        traps={
            "symlink": traps / "live-claude",
            "parent": root / "claude" / ".." / ".." / "escape",
            "environ": REAL_HOME / ".codex",
        },
        **homes,
    )


def confine_environ(identities: Identities, environ) -> None:
    for name in identities.environ():
        value = environ.get(name, "")
        if name.endswith("_URL"):
            value = value.removeprefix("unix://").split("?", 1)[0]
        if name != "REDIS_KEY_PREFIX":
            confine(identities.root, name, value)
        elif value != identities.redis_prefix:
            refuse("redis", f"REDIS_KEY_PREFIX is not the fixture run prefix {identities.redis_prefix}")


def owned(identities: Identities, key: str) -> str:
    if not key.startswith(identities.redis_prefix + ":"):
        refuse("redis", f"key {key} is outside fixture run {identities.run_id}")
    return key


def acquire(redis: "Redis", identities: Identities, name: str, holder: str, ttl_ms: int) -> bool:
    redis.set(identities.key("owner"), identities.run_id, nx=True)
    return bool(redis.set(owned(identities, identities.key("lock", name)), holder, nx=True, px=ttl_ms))


def sweep(redis: "Redis", identities: Identities) -> list[str]:
    if redis.get(identities.key("owner")) != identities.run_id:
        refuse("redis", f"run {identities.run_id} does not own the keys under {identities.redis_prefix}")
    keys = sorted(redis.scan_iter(match=f"{identities.redis_prefix}:*"))
    if keys:
        redis.delete(*keys)
    return keys


def remove(identities: Identities) -> None:
    root = identities.root.resolve()
    marker = root / MARKER
    if any(root.is_relative_to(live) for live in LIVE_ROOTS):
        refuse("cleanup", f"{root} is a live root")
    if not marker.is_file() or marker.read_text() != identities.run_id:
        refuse("cleanup", f"{root} does not belong to fixture run {identities.run_id}")
    shutil.rmtree(root)


def _shell_command(argv: list[str]) -> str | None:
    if Path(argv[0]).name not in SHELLS | WRAPPERS:
        return None
    start = next((i for i, arg in enumerate(argv) if Path(arg).name in SHELLS), None)
    if start is None:
        return None
    flags = argv[start + 1 :]
    index = next((i for i, flag in enumerate(flags) if SHELL_COMMAND_FLAG.fullmatch(flag)), None)
    return flags[index + 1] if index is not None and index + 1 < len(flags) else None


def _programs(argv) -> list[str]:
    if isinstance(argv, (str, bytes, os.PathLike)):
        argv = [argv]
    argv = [os.fsdecode(arg) for arg in argv]
    if not argv:
        return []
    words = argv if Path(argv[0]).name in WRAPPERS else argv[:1]
    command = _shell_command(argv)
    if command is not None:
        try:
            spliced = shlex.split(command)
        except ValueError:
            spliced = [command]
        words = words + [word for part in spliced for word in TOKENS.split(part)]
    return [word for word in words if word]


def _resolve(program: str, env) -> Path | None:
    if os.sep not in program:
        program = shutil.which(program, path=os.pathsep.join(os.get_exec_path(env))) or ""
    return Path(program).resolve() if program and Path(program).exists() else None


def refuse_live_program(event: str, args: tuple) -> None:
    if event not in SPAWN_EVENTS or FIXTURE_ROOT is None:
        return
    if event == "os.system":
        executable, argv, env = None, ["sh", "-c", os.fsdecode(args[0])], None
    elif event == "subprocess.Popen":
        executable, argv, env = args[0], args[1], args[3]
    else:
        executable, argv, env = args[:3]
    for program in _programs(argv) + ([os.fsdecode(executable)] if executable else []):
        if Path(program).name not in LIVE_PROGRAMS:
            continue
        resolved = _resolve(program, env)
        if resolved is not None and not resolved.is_relative_to(FIXTURE_ROOT):
            refuse("program", f"refusing a live {Path(program).name} outside the test directory: {resolved}")


sys.addaudithook(refuse_live_program)
