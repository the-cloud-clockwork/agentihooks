"""One supervised Codex app-server per admitted execution: admit, spawn, connect, bind, then open exactly once."""

import contextlib
import fcntl
import json
import os
import shutil
import socket
import subprocess
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from hooks.observability.trace_flush import start_time
from scripts.codex_session import protocol
from scripts.codex_session.binding import CONNECTED, EXITED, FAILED, RECORDS, BindingStore, TransportRecord, decode
from scripts.profiles import binding
from scripts.routing import envs
from scripts.swarm.store import APP_SERVER_TRANSPORT, MASTER, AgentRecord, RedisStore
from scripts.swarm_v2 import masters

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
SHARED_ROLLOUTS = ("sessions", "archived_sessions")
OMITTED_STATE = (*SHARED_ROLLOUTS, "bin", "history.jsonl", "session_index.jsonl", "log", "tmp", "shell_snapshots")
RUNTIME_SUBDIR = "cx"
HOME_SUBDIR = ".agentihooks"
CODEX_HOME_KEY = "codexHome"
OPENING_ATTEMPTS = 2
OPENING_WATCH_S = 30.0
TURN_COMPLETED = "turn/completed"
USER_MESSAGE = "userMessage"
INPUT_ITEMS = (USER_MESSAGE, "hookPrompt")
CAPACITY = "Selected model is at capacity"

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
OPENING_CAPACITY = "the opening Codex turn ended twice on a provider capacity error: {}"


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
    return Path(environ.get("HOME") or Path.home()) / HOME_SUBDIR / RUNTIME_SUBDIR


def socket_path(root: Path, execution_id: str) -> Path:
    directory = root / execution_id.removeprefix(EXECUTION_PREFIX)[:ENDPOINT_ID_CHARS]
    path = directory / SOCKET_NAME
    if len(str(path).encode()) > SOCKET_PATH_MAX:
        raise LaunchFailure("endpoint", LONG_SOCKET)
    for private in (root, directory):
        private.mkdir(parents=True, exist_ok=True)
        try:
            private.chmod(PRIVATE_MODE)
        except BaseException:
            if private == directory:
                shutil.rmtree(directory)
            raise
    return path


def execution_directory(record: TransportRecord) -> Path | None:
    directory = Path(record.endpoint_ref.removeprefix(UNIX)).parent
    if directory.parent.name != RUNTIME_SUBDIR:
        return None
    if directory.name != record.execution_id.removeprefix(EXECUTION_PREFIX)[:ENDPOINT_ID_CHARS]:
        return None
    return directory


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
    from scripts.swarm.commands import hive_id

    agent = replace(launch.agent, harness="codex", transport=chosen, hive=hive_id(), runtime_target=target)
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


def shown_name(launch: Launch, agent: AgentRecord) -> str:
    title = masters.title(launch.slug, agent.seat)
    return title if agent.lane == MASTER else agent.name


def name_thread(connection: protocol.Connection, thread_id: str, name: str) -> None:
    """A refused name leaves the default one; the launch still opens its turn."""
    with contextlib.suppress(protocol.Refused):
        protocol.set_thread_name(connection, thread_id, name)


def opening_end(connection: protocol.Connection, events: list[dict], deadline: float, clock: Callable) -> dict:
    """The opening turn as it completed; empty once the model answers or the watch ends first."""
    while True:
        while events:
            message = events.pop(0)
            params = message.get("params") or {}
            if message["method"] == TURN_COMPLETED:
                return params["turn"]
            if params.get("item", {}).get("type", USER_MESSAGE) not in INPUT_ITEMS:
                return {}
        if not connection.poll(deadline - clock()):
            return {}


def open_turn(connection: protocol.Connection, thread_id: str, launch: Launch, deps: Deps) -> str:
    for _ in range(OPENING_ATTEMPTS):
        events: list[dict] = []
        connection.listener = events.append
        turn_id = protocol.start_turn(connection, thread_id, launch.opening)
        turn = opening_end(connection, events, deps.clock() + OPENING_WATCH_S, deps.clock)
        error = str((turn.get("error") or {}).get("message"))
        if CAPACITY not in error:
            return turn_id
    reason = OPENING_CAPACITY.format(error)
    if launch.environ.get(binding.REPORT):
        binding.refuse(Path(launch.environ[binding.REPORT]), reason)
    raise LaunchFailure("capacity", reason)


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
        name_thread(connection, thread["id"], shown_name(launch, agent))
        if launch.opening and bindings.claim_opening(launch.slug, agent.execution_id, deps.now_ms()):
            record = replace(record, active_turn_id=open_turn(connection, thread["id"], launch, deps))
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
    shutil.copytree(
        source,
        home,
        ignore=lambda directory, names: (
            tuple(name for name in names if name in OMITTED_STATE or ".sqlite" in name)
            if Path(directory) == source
            else ()
        ),
        ignore_dangling_symlinks=True,
    )
    for name in SHARED_ROLLOUTS:
        canonical = (source / name).resolve()
        canonical.mkdir(parents=True, exist_ok=True)
        (home / name).symlink_to(canonical)
    binaries = source / "bin"
    if binaries.exists():
        (home / "bin").symlink_to(binaries.resolve())
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
    if spec.environ.get(binding.REPORT):
        binding.rehome(Path(spec.environ[binding.REPORT]), home)
    return replace(spec, codex_home=str(home), environ={**spec.environ, "CODEX_HOME": str(home)})


def preserve_legacy_rollouts(home: Path) -> None:
    legacy = [home / name for name in SHARED_ROLLOUTS if not (home / name).is_symlink() and (home / name).is_dir()]
    if not legacy:
        return
    config = home / "config.toml"
    settings = tomllib.loads(config.read_text()) if config.is_file() else {}
    canonical = Path(settings.get("sqlite_home") or Path.home() / ".codex")
    if canonical.is_relative_to(home):
        canonical = Path.home() / ".codex"
    for sessions in legacy:
        for rollout in sessions.rglob("*.jsonl"):
            destination = canonical / sessions.name / rollout.relative_to(sessions)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with rollout.open("rb") as incoming, destination.open("a+b") as outgoing:
                fcntl.flock(outgoing.fileno(), fcntl.LOCK_EX)
                outgoing.seek(0)
                previous, current = outgoing.read(), incoming.read()
                if current.startswith(previous):
                    outgoing.write(current[len(previous) :])
                elif not previous.startswith(current):
                    recovered = (
                        canonical / sessions.name / f"recovered-{home.parent.name}" / rollout.relative_to(sessions)
                    )
                    recovered.parent.mkdir(parents=True, exist_ok=True)
                    recovered.write_bytes(current)


def remove_home(directory: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        preserve_legacy_rollouts(directory / "codex")
        shutil.rmtree(directory)


def reclaim_homes(store: RedisStore, slug: str, deps: Deps) -> None:
    bindings = BindingStore(store)
    for raw in store.redis.hgetall(store.key(slug, RECORDS)).values():
        record = decode(raw)
        directory = execution_directory(record)
        if record.host_id != socket.gethostname() or directory is None or not directory.exists():
            continue
        if record.server_pid:
            identity = deps.start_identity(record.server_pid)
            alive = identity is not None and (
                not record.server_start_identity or identity == record.server_start_identity
            )
        else:
            bridge = record.bridge_owner.rpartition(":")[2]
            alive = not bridge.isdecimal() or deps.start_identity(int(bridge)) is not None
        if alive:
            continue
        remove_home(directory)
        ended = replace(record, state=EXITED, active_turn_id="")
        bindings.put(slug, ended)
        bindings.release(slug, ended)


def launch(store: RedisStore, spec: Launch, deps: Deps) -> Started:
    check_environment(spec.environ, spec.allowed_tokens)
    reclaim_homes(store, spec.slug, deps)
    agent = admit(store, spec, deps)
    bindings = BindingStore(store)
    record, process, path = None, None, None
    try:
        path = socket_path(deps.runtime_root or runtime_root(spec.environ), agent.execution_id)
        record = bindings.reserve(spec.slug, _record(spec, agent, path))
        spec = execution_home(spec, path.parent)
        process = spawn(spec, deps, path)
        record = replace(record, server_pid=process.pid, server_start_identity=deps.start_identity(process.pid) or 0)
        bindings.put(spec.slug, record)
        record, agent = _connect_bound(store, bindings, spec, agent, record, process, deps)
    except BaseException as failure:
        try:
            if process is not None:
                stop(process)
        finally:
            try:
                if record is not None:
                    error_class = getattr(failure, "error_class", type(failure).__name__)
                    bindings.put(spec.slug, replace(record, state=FAILED, last_error_class=error_class))
                    bindings.release(spec.slug, record)
            finally:
                try:
                    store.put_agent(spec.slug, replace(agent, state="finished"))
                    store.drop_agent(spec.slug, agent.name, deps.now_ms())
                finally:
                    if path is not None:
                        shutil.rmtree(path.parent)
        raise
    return Started(record, agent, process)


def finish(store: RedisStore, slug: str, started: Started, now_ms: int) -> TransportRecord:
    bindings = BindingStore(store)
    current = bindings.get(slug, started.record.execution_id) or started.record
    record = replace(current, state=EXITED, active_turn_id="", last_transport_activity=now_ms)
    bindings.put(slug, record)
    bindings.release(slug, record)
    directory = execution_directory(record)
    if directory is not None:
        remove_home(directory)
    return record
