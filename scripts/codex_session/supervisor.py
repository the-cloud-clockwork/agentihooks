"""One supervised Codex app-server per admitted execution: admit, spawn, connect, bind, then open exactly once."""

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from hooks.observability.trace_flush import start_time
from scripts.codex_session import protocol
from scripts.codex_session.binding import CONNECTED, EXITED, FAILED, BindingStore, TransportRecord
from scripts.profiles import binding
from scripts.routing import envs
from scripts.swarm.store import APP_SERVER_TRANSPORT, AgentRecord, RedisStore

NO_DAEMON = "--no-daemon"
REQUIRED_HOOKS = ("sessionStart", "userPromptSubmit", "preToolUse", "postToolUse", "stop")
TRUSTED = "trusted"
STARTUP_TIMEOUT_S = 30.0
POLL_S = 0.25
MAX_POLLS = 10_000
STOP_WAIT_S = 5.0
# Linux sun_path holds 108 bytes including the terminator; macOS holds 104.
SOCKET_PATH_MAX = 100
SOCKET_NAME = "s"
UNIX = "unix://"
EXECUTION_PREFIX = "exe-"
ENDPOINT_ID_CHARS = 16
PRIVATE_MODE = 0o700
RUNTIME_DIR_ENV = "XDG_RUNTIME_DIR"
RUNTIME_SUBDIR = "cx"
HOME_SUBDIR = ".agentihooks"
CODEX_HOME_KEY = "codexHome"

LONG_SOCKET = f"socket path is longer than {SOCKET_PATH_MAX} bytes"
DAEMON_REFUSED = "app-server takes no --no-daemon flag"
FOREIGN_CREDENTIALS = "child environment carries other account credentials: {}"
OTHER_TRANSPORT = "swarm {} runs the {} Codex inbox transport"
EXITED_EARLY = "app-server exited with status {} before it answered"
PID_TAKEN = "the server pid no longer belongs to the started process"
NEVER_ANSWERED = "app-server started but never answered initialize in time"
HOOKS_MISSING = "required profile hooks are missing: {}"
HOOKS_UNTRUSTED = "required hooks stay untrusted: {}"
OTHER_HOME = "the server runs from another Codex home than the rendered profile"
OTHER_ACCOUNT = "the server is not signed in with a {} account"
NO_THREAD = "the server returned no thread id"
SPAWN_FAILED = "app-server could not start: {}"


class LaunchFailure(RuntimeError):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


@dataclass(frozen=True)
class Launch:
    slug: str
    agent: AgentRecord
    cwd: str
    codex_home: str
    environ: Mapping[str, str]
    server_args: tuple = ()
    previous_execution_id: str = ""
    resume_thread: str = ""
    opening: str = ""
    account_type: str = ""
    allowed_tokens: frozenset = frozenset()
    bundle_revision: str = ""
    startup_timeout_s: float = STARTUP_TIMEOUT_S


def time_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Deps:
    popen: Callable = subprocess.Popen
    connect: Callable = protocol.connect
    start_identity: Callable[[int], int | None] = start_time
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    now_ms: Callable[[], int] = time_ms
    codex_bin: str = "codex"
    runtime_root: Path | None = None


@dataclass(frozen=True)
class Started:
    record: TransportRecord
    agent: AgentRecord
    process: subprocess.Popen


def runtime_root(environ: Mapping[str, str]) -> Path:
    runtime = environ.get(RUNTIME_DIR_ENV)
    base = Path(runtime) if runtime else Path(environ.get("HOME") or Path.home()) / HOME_SUBDIR
    return base / RUNTIME_SUBDIR


def socket_path(root: Path, execution_id: str) -> Path:
    directory = root / execution_id.removeprefix(EXECUTION_PREFIX)[:ENDPOINT_ID_CHARS]
    path = directory / SOCKET_NAME
    if len(str(path).encode()) > SOCKET_PATH_MAX:
        raise LaunchFailure("endpoint", LONG_SOCKET)
    for private in (root, directory):
        private.mkdir(parents=True, exist_ok=True)
        private.chmod(PRIVATE_MODE)
    return path


def server_command(codex_bin: str, path: Path, args: tuple) -> list[str]:
    if NO_DAEMON in args:
        raise LaunchFailure("command", DAEMON_REFUSED)
    return [codex_bin, "app-server", "--listen", f"{UNIX}{path}", *args]


def owns(record: TransportRecord, start_identity: Callable[[int], int | None]) -> bool:
    if record.server_pid < 1 or record.server_start_identity < 1:
        return False
    return start_identity(record.server_pid) == record.server_start_identity


def check_environment(environ: Mapping[str, str], allowed: frozenset) -> None:
    foreign = sorted(name for name in environ if name.startswith(envs.CODEX_TOKEN_PREFIX) and name not in allowed)
    if foreign:
        raise LaunchFailure("wrong_account", FOREIGN_CREDENTIALS.format(foreign))


def admit(store: RedisStore, launch: Launch, deps: Deps) -> AgentRecord:
    chosen = store.config(launch.slug).codex_inbox_transport
    if chosen != APP_SERVER_TRANSPORT:
        raise LaunchFailure("transport", OTHER_TRANSPORT.format(launch.slug, chosen))
    pid_start = deps.start_identity(os.getpid())
    target = {"pid": os.getpid(), "pid_start": pid_start} if pid_start else {}
    agent = replace(launch.agent, harness="codex", transport=chosen, runtime_target=target)
    return store.start_execution(launch.slug, agent, launch.previous_execution_id)


def wait_ready(
    path: Path, process: subprocess.Popen, record: TransportRecord, launch: Launch, deps: Deps
) -> tuple[protocol.Connection, dict]:
    deadline = deps.clock() + launch.startup_timeout_s
    for _ in range(MAX_POLLS):
        left = deadline - deps.clock()
        if left <= 0:
            break
        if process.poll() is not None:
            raise LaunchFailure("server_exited", EXITED_EARLY.format(process.returncode))
        if not owns(record, deps.start_identity):
            raise LaunchFailure("server_identity", PID_TAKEN)
        if path.exists():
            try:
                return deps.connect(str(path), left)
            except (OSError, TimeoutError, protocol.ProtocolError):
                pass
        deps.sleep(POLL_S)
    raise LaunchFailure("startup_timeout", NEVER_ANSWERED)


def profile_hooks(connection: protocol.Connection, cwd: str, codex_home: str) -> list[dict]:
    home = Path(os.path.realpath(codex_home))
    listed = protocol.hooks(connection, cwd)
    return [hook for hook in listed if home in Path(os.path.realpath(hook.get("sourcePath") or "/")).parents]


def ensure_hooks(connection: protocol.Connection, cwd: str, codex_home: str) -> None:
    owned = profile_hooks(connection, cwd, codex_home)
    missing = [event for event in REQUIRED_HOOKS if event not in {hook.get("eventName") for hook in owned}]
    if missing:
        raise LaunchFailure("hooks_missing", HOOKS_MISSING.format(missing))
    untrusted = [hook for hook in owned if hook.get("trustStatus") != TRUSTED]
    if untrusted:
        protocol.trust(connection, untrusted)
        relisted = profile_hooks(connection, cwd, codex_home)
        still = {hook.get("eventName") for hook in relisted if hook.get("trustStatus") != TRUSTED}
        if still & set(REQUIRED_HOOKS):
            raise LaunchFailure("hooks_untrusted", HOOKS_UNTRUSTED.format(sorted(still)))


def check_binding(connection: protocol.Connection, info: dict, launch: Launch) -> None:
    home = info.get(CODEX_HOME_KEY)
    if not home or os.path.realpath(home) != os.path.realpath(launch.codex_home):
        raise LaunchFailure("wrong_profile", OTHER_HOME)
    ensure_hooks(connection, launch.cwd, launch.codex_home)
    if launch.account_type and protocol.account_type(connection) != launch.account_type:
        raise LaunchFailure("wrong_account", OTHER_ACCOUNT.format(launch.account_type))


def bind_thread(connection: protocol.Connection, launch: Launch) -> dict:
    thread = (
        protocol.resume_thread(connection, launch.resume_thread)
        if launch.resume_thread
        else protocol.start_thread(connection, launch.cwd)
    )
    if not thread.get("id"):
        raise LaunchFailure("protocol", NO_THREAD)
    return thread


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _record(launch: Launch, agent: AgentRecord, path: Path) -> TransportRecord:
    host = socket.gethostname()
    return TransportRecord(
        execution_id=agent.execution_id,
        execution_generation=agent.generation,
        seat=agent.seat,
        agent_name=agent.name,
        task_id=agent.task,
        endpoint_ref=f"{UNIX}{path}",
        host_id=host,
        account=agent.account,
        profile=agent.profile,
        bundle_revision=launch.bundle_revision,
        model=agent.model,
        effort=agent.effort,
        bridge_owner=f"{host}:{os.getpid()}",
    )


def _connect_bound(store, bindings, launch, agent, record, process, deps) -> tuple[TransportRecord, AgentRecord]:
    path = Path(record.endpoint_ref.removeprefix(UNIX))
    connection, info = wait_ready(path, process, record, launch, deps)
    try:
        check_binding(connection, info, launch)
        thread = bind_thread(connection, launch)
        record = replace(
            record,
            thread_id=thread["id"],
            session_id=thread.get("sessionId") or "",
            state=CONNECTED,
            last_transport_activity=deps.now_ms(),
        )
        bindings.put(launch.slug, record)
        agent = replace(agent, conversation_id=thread["id"])
        store.put_agent(launch.slug, agent)
        if launch.opening and bindings.claim_opening(launch.slug, agent.execution_id, deps.now_ms()):
            record = replace(record, active_turn_id=protocol.start_turn(connection, thread["id"], launch.opening))
            bindings.put(launch.slug, record)
        return record, agent
    except (OSError, TimeoutError, protocol.ProtocolError) as exc:
        raise LaunchFailure("protocol", str(exc)) from exc
    finally:
        connection.close()


def spawn(spec: Launch, deps: Deps, path: Path) -> subprocess.Popen:
    try:
        return deps.popen(
            server_command(deps.codex_bin, path, spec.server_args),
            env=dict(spec.environ),
            cwd=spec.cwd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise LaunchFailure("spawn", SPAWN_FAILED.format(exc)) from exc


def execution_home(spec: Launch, directory: Path) -> Launch:
    source = Path(spec.codex_home)
    home = directory / "codex"
    shutil.copytree(source, home, ignore_dangling_symlinks=True)
    home.chmod(PRIVATE_MODE)
    for name in ("codex.sources.json", "codex.mounts.json"):
        manifest = source.parent / name
        if manifest.is_file():
            shutil.copyfile(manifest, directory / name)
    evidence = home / binding.FILE
    if evidence.is_file():
        data = json.loads(evidence.read_text())
        data["home"] = str(home.resolve())
        evidence.write_text(json.dumps(data) + "\n")
    return replace(spec, codex_home=str(home), environ={**spec.environ, "CODEX_HOME": str(home)})


def launch(store: RedisStore, spec: Launch, deps: Deps) -> Started:
    check_environment(spec.environ, spec.allowed_tokens)
    agent = admit(store, spec, deps)
    bindings = BindingStore(store)
    path = socket_path(deps.runtime_root or runtime_root(spec.environ), agent.execution_id)
    record = bindings.reserve(spec.slug, _record(spec, agent, path))
    process = None
    try:
        spec = execution_home(spec, path.parent)
        process = spawn(spec, deps, path)
        record = replace(record, server_pid=process.pid, server_start_identity=deps.start_identity(process.pid) or 0)
        bindings.put(spec.slug, record)
        record, agent = _connect_bound(store, bindings, spec, agent, record, process, deps)
    except BaseException as failure:
        if process is not None:
            stop(process)
        error_class = getattr(failure, "error_class", type(failure).__name__)
        bindings.put(spec.slug, replace(record, state=FAILED, last_error_class=error_class))
        bindings.release(spec.slug, record)
        raise
    return Started(record, agent, process)


def finish(store: RedisStore, slug: str, started: Started, now_ms: int) -> TransportRecord:
    bindings = BindingStore(store)
    current = bindings.get(slug, started.record.execution_id) or started.record
    record = replace(current, state=EXITED, active_turn_id="", last_transport_activity=now_ms)
    bindings.put(slug, record)
    bindings.release(slug, record)
    return record
