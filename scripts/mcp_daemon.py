"""Lifecycle for the agentihooks MCP daemon.

Only network transports (``sse``, ``streamable-http``) need a persistent server:
under stdio Claude Code spawns one process per session and there is nothing to
supervise. Everything here is inert on a stdio machine.

Two backends, auto-selected:

- **systemd** where a user session exists — the unit ``agentihooks init`` renders
  is the supervisor, so restarts survive crashes and reboots.
- **pidfile** everywhere else — WSL2 without ``systemd=true``, containers, macOS.
  A detached child plus a recorded pid. No supervisor, so it does not survive a
  reboot; that is the honest limit of the fallback, not a bug to paper over.

This module deliberately does not import ``hooks.*``. ``scripts/install.py`` has
that constraint and this is part of its dependency surface.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def state_dir() -> Path:
    """Resolved per call, not bound at import.

    ``AGENTIHOOKS_HOME`` and ``Path.home()`` are both things a caller can change
    after this module is imported — the installer does exactly that, and so do the
    tests. A module-level constant silently keeps pointing at whatever the value
    was when the first import happened.
    """
    return Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))


def pidfile() -> Path:
    return state_dir() / "mcp-daemon.pid"


def logfile() -> Path:
    return state_dir() / "logs" / "mcp-daemon.log"


def _lockfile() -> Path:
    return state_dir() / "mcp-daemon.lock"


UNIT_NAME = "agentihooks-mcp.service"

NETWORK_TRANSPORTS = ("sse", "streamable-http")

# The process is identified by its module path, not by the interpreter that runs
# it — the venv python differs per machine, `-m hooks.mcp` does not.
_PROC_MARKER = "hooks.mcp"

_SYSTEMCTL_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class DaemonState:
    """Outcome of a start/restart attempt."""

    running: bool
    backend: str  # "systemd" | "pidfile" | "none"
    pid: int | None = None
    transport: str = ""
    detail: str = ""


@dataclass
class DaemonStatus:
    """Everything needed to tell working from broken from diverged."""

    backend: str
    running: bool
    pid: int | None = None
    live_transport: str = ""
    live_host: str = ""
    live_port: int | None = None
    configured_transport: str = ""
    configured_host: str = ""
    configured_port: int | None = None
    client_entry: dict | None = None
    port_open: bool = False
    liveness_is_exact: bool = True
    endpoint_check_is_exact: bool = True
    divergences: list[str] = field(default_factory=list)

    @property
    def diverged(self) -> bool:
        return bool(self.divergences)

    def exit_code(self) -> int:
        """0 running and matching, 1 stopped, 2 diverged."""
        if not self.running:
            return 1
        return 2 if self.diverged else 0


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Best-effort TCP probe. Never a gate — the daemon may not be started yet."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def _lock():
    """Serialise start/stop so two invocations cannot both spawn a daemon."""
    if fcntl is None:
        yield
        return
    _lockfile().parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = open(_lockfile(), "w")  # noqa: SIM115
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def _read_cmdline(pid: int) -> bytes:
    """Raw /proc cmdline, or b"" where /proc is unavailable."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def _proc_fs_available() -> bool:
    return Path("/proc/self/cmdline").exists()


def pid_alive(pid: int | None) -> bool:
    """Is *pid* a live agentihooks MCP daemon?

    The cmdline cross-check is what stops a recycled pid reading as a running
    daemon. Where /proc is absent the signal check stands alone and callers are
    told via ``DaemonStatus.liveness_is_exact``.
    """
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    if not _proc_fs_available():
        return True
    # `-m hooks.mcp` as an adjacent argv pair, which is exactly what both the
    # systemd unit and _spawn_detached run. A substring test on the raw blob also
    # matches a path containing "hooks.mcp" or a future `hooks.mcp_reporter`; a
    # bare whole-argv test still matches `grep hooks.mcp`. A recycled pid landing
    # on any of those would make `stop` SIGTERM an unrelated process.
    argv = _read_cmdline(int(pid)).split(b"\0")
    marker = _PROC_MARKER.encode()
    return any(a == b"-m" and b == marker for a, b in zip(argv, argv[1:]))


# ---------------------------------------------------------------------------
# Pidfile
# ---------------------------------------------------------------------------


def read_pidfile() -> dict:
    try:
        data = json.loads(pidfile().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_pidfile(record: dict) -> None:
    pidfile().parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp name per writer — a fixed ".tmp" is shared by every concurrent
    # writer and races os.replace.
    tmp = pidfile().with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(pidfile()))


def _clear_pidfile() -> None:
    with contextlib.suppress(OSError):
        pidfile().unlink()


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _systemctl_usable() -> bool:
    """Is there a systemd *user* session we can drive?

    ``systemctl --user is-active`` cannot answer this — it also exits non-zero for
    a unit that merely is not running. ``show-environment`` fails only when the
    user bus itself is unreachable, which is the WSL2-without-systemd case
    ("Failed to connect to bus: No medium found").
    """
    if not shutil.which("systemctl"):
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            timeout=_SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def unit_installed() -> bool:
    return (Path.home() / ".config" / "systemd" / "user" / UNIT_NAME).exists()


VALID_SUPERVISORS = ("auto", "systemd", "pidfile")


def resolve_backend() -> str:
    """``systemd`` | ``pidfile``. ``AGENTIHOOKS_MCP_SUPERVISOR`` forces either.

    An unrecognised value warns and falls back to probing, matching how
    ``MCP_TRANSPORT`` behaves. Silently ignoring a typo would mean an operator who
    wrote ``pid-file`` to pin the fallback gets whatever probing decides, with no
    hint that their setting did nothing.
    """
    forced = os.environ.get("AGENTIHOOKS_MCP_SUPERVISOR", "auto").strip().lower()
    if forced in ("systemd", "pidfile"):
        return forced
    if forced and forced != "auto":
        print(
            f"[agentihooks] WARNING: unknown AGENTIHOOKS_MCP_SUPERVISOR={forced!r}; "
            f"detecting instead. Valid values: {', '.join(VALID_SUPERVISORS)}.",
            file=sys.stderr,
        )
    return "systemd" if _systemctl_usable() else "pidfile"


# ---------------------------------------------------------------------------
# Configuration — mirrors what the installer writes into ~/.claude.json
# ---------------------------------------------------------------------------


def _env_files() -> list[Path]:
    """Every file ``hooks/config.py::_load_user_env`` reads, in its order.

    ``.env`` first, then the other ``*.env`` companions sorted — the sharded
    layout used for per-container config. Scanning only ``.env`` was a real bug:
    a transport set in a companion file reached the daemon (which imports
    ``hooks.config``) but not the lifecycle commands, which then refused to manage
    a daemon that was in fact running in network mode.
    """
    files = [state_dir() / ".env"]
    if state_dir().is_dir():
        files += [p for p in sorted(state_dir().glob("*.env")) if p.name != ".env"]
    return files


def _scan_env_file(key: str) -> str | None:
    """Read *key* from the ~/.agentihooks env files.

    Mirrors ``hooks/config.py`` — ``export`` prefixes, quoted values, inline
    comments — and first-assignment-wins across the whole set, because that loader
    populates with ``os.environ.setdefault``. Pinned by
    ``tests/test_mcp_daemon.py::TestEnvParserParity``; the two drifting apart means
    the daemon and the tooling that manages it disagree about config.
    """
    for env_file in _env_files():
        try:
            raw_text = env_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in raw_text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            name, _, val = line.partition("=")
            if name.strip() != key:
                continue
            val = val.strip()
            if val and val[0] in ('"', "'"):
                quote = val[0]
                end = val.find(quote, 1)
                return val[1:end] if end != -1 else val[1:]
            if "#" in val:
                return val[: val.index("#")].rstrip()
            return val
    return None


def _config_value(key: str, default: str) -> str:
    env = os.environ.get(key, "").strip()
    if env:
        return env
    from_file = _scan_env_file(key)
    return from_file.strip() if from_file else default


def configured_transport() -> str:
    forced = os.environ.get("AGENTIHOOKS_MCP_TRANSPORT", "").strip().lower()
    if forced:
        return forced
    return _config_value("MCP_TRANSPORT", "stdio").lower()


def configured_host() -> str:
    return _config_value("MCP_HOST", "localhost")


def configured_port() -> int:
    try:
        return int(_config_value("MCP_PORT", "8642"))
    except ValueError:
        return 8642


def _client_entry() -> dict | None:
    """The agentihooks MCP entry as ~/.claude.json actually declares it."""
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    from scripts.targets._common import MCP_SERVER_NAME

    entry = (data.get("mcpServers") or {}).get(MCP_SERVER_NAME)
    return entry if isinstance(entry, dict) else None


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------


def _child_env(transport: str) -> dict:
    """Environment for the spawned daemon.

    ``MCP_TRANSPORT`` is set explicitly rather than inherited: a stray
    ``export MCP_TRANSPORT=…`` in the invoking shell must not be able to diverge
    the daemon from what the installer validated and wrote into the client config.
    This is the same reasoning that keeps ``Environment=`` rather than
    ``EnvironmentFile=`` in the systemd unit.
    """
    env = dict(os.environ)
    env["MCP_TRANSPORT"] = transport
    return env


_LOG_MAX_BYTES = 5 * 1024 * 1024


def _roll_log_if_large() -> None:
    """Keep one previous generation, discard older.

    Nothing else rotates this file — ``_clean_state_dir``'s delete whitelist does
    not cover ``logs/`` — and the daemon appends for the life of the machine. A
    restart is the natural rotation point.
    """
    path = logfile()
    try:
        if path.stat().st_size < _LOG_MAX_BYTES:
            return
        os.replace(str(path), str(path.with_suffix(".log.1")))
    except OSError:
        pass


def _spawn_detached(python: str, transport: str, cwd: str) -> int:
    logfile().parent.mkdir(parents=True, exist_ok=True)
    _roll_log_if_large()
    log = open(logfile(), "ab")  # noqa: SIM115 - handed to the child, closed below
    try:
        proc = subprocess.Popen(  # noqa: S603
            [python, "-m", "hooks.mcp"],
            cwd=cwd,
            env=_child_env(transport),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    finally:
        log.close()
    return proc.pid


def _stop_pidfile_backend() -> bool:
    """Terminate a recorded daemon. True if something was actually stopped."""
    record = read_pidfile()
    pid = record.get("pid")
    if not pid_alive(pid):
        _clear_pidfile()
        return False

    pid = int(pid)
    try:
        os.kill(pid, 15)
    except OSError:
        _clear_pidfile()
        return False

    for _ in range(50):  # up to ~5s
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, 9)

    _clear_pidfile()
    return True


def _stop_systemd_backend() -> bool:
    if not unit_installed() or not _systemctl_usable():
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "stop", UNIT_NAME],
            capture_output=True,
            timeout=_SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _stop_unlocked() -> bool:
    return _stop_pidfile_backend() | _stop_systemd_backend()


def stop() -> bool:
    """Stop the daemon under BOTH backends.

    Not just the currently-detected one: a machine that had systemd and lost it —
    or that ran the fallback before the unit existed — can hold state in both, and
    tearing down only one leaves a process serving a port nothing points at.
    """
    with _lock():
        return _stop_unlocked()


def _already_running() -> bool:
    if resolve_backend() == "systemd":
        return _systemd_unit_active()
    return pid_alive(read_pidfile().get("pid"))


def ensure_running(transport: str, python: str, cwd: str, *, restart: bool = True) -> DaemonState:
    """Bring the daemon up on *transport*, restarting a running one.

    ``restart=True`` is unconditional by design: after ``agentihooks init`` the
    running process must serve the config just written, and comparing every input
    that could have changed is more failure surface than a restart costs.

    The whole decide-stop-spawn-record sequence runs under one lock. Checking
    liveness outside it was a real race: two concurrent starts both saw "not
    running", both spawned, and the loser — whose bind failed on the taken port —
    still overwrote the pidfile with its own pid before dying. The pidfile then
    named a corpse while the live daemon went untracked, so `stop` reported
    "nothing running" and the real process could not be killed through the CLI.
    """
    if transport not in NETWORK_TRANSPORTS:
        stop()
        return DaemonState(running=False, backend="none", transport=transport, detail="stdio — no daemon needed")

    with _lock():
        if restart:
            _stop_unlocked()
        elif _already_running():
            pid = read_pidfile().get("pid")
            return DaemonState(
                running=True,
                backend=resolve_backend(),
                pid=int(pid) if pid_alive(pid) else None,
                transport=transport,
                detail="already running",
            )

        if resolve_backend() == "systemd":
            return _start_systemd(transport)
        return _start_pidfile(transport, python, cwd)


def _start_systemd(transport: str) -> DaemonState:
    if not unit_installed():
        return DaemonState(
            running=False,
            backend="systemd",
            transport=transport,
            detail=f"unit {UNIT_NAME} not installed",
        )
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "enable", "--now", UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DaemonState(running=False, backend="systemd", transport=transport, detail=str(exc))

    if proc.returncode != 0:
        return DaemonState(
            running=False,
            backend="systemd",
            transport=transport,
            detail=(proc.stderr or proc.stdout or "").strip(),
        )
    return DaemonState(running=True, backend="systemd", transport=transport, detail=f"{UNIT_NAME} active")


def _start_pidfile(transport: str, python: str, cwd: str) -> DaemonState:
    """Spawn and record. Caller holds the lock — see ``ensure_running``."""
    host, port = configured_host(), configured_port()
    try:
        pid = _spawn_detached(python, transport, cwd)
    except OSError as exc:
        return DaemonState(running=False, backend="pidfile", transport=transport, detail=str(exc))

    _write_pidfile(
        {
            "pid": pid,
            "transport": transport,
            "host": host,
            "port": port,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "argv": [python, "-m", "hooks.mcp"],
        }
    )

    # Liveness is checked before the port, deliberately. An open port is not proof
    # that OUR child is serving it — a daemon we failed to stop, or another
    # process, answers identically. Our own pid dying is unambiguous.
    for _ in range(40):  # up to ~4s
        if not pid_alive(pid):
            _clear_pidfile()
            return DaemonState(
                running=False,
                backend="pidfile",
                transport=transport,
                detail=f"daemon exited immediately — see {logfile()}",
            )
        if port_open(host, port, timeout=0.25):
            return DaemonState(running=True, backend="pidfile", pid=pid, transport=transport, detail=f"{host}:{port}")
        time.sleep(0.1)

    return DaemonState(
        running=True,
        backend="pidfile",
        pid=pid,
        transport=transport,
        detail=f"started (pid {pid}) but {host}:{port} not answering yet",
    )


def restart(transport: str, python: str, cwd: str) -> DaemonState:
    return ensure_running(transport, python, cwd, restart=True)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _systemd_unit_active() -> bool:
    if not _systemctl_usable():
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT_NAME],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.strip() == "active"


def status() -> DaemonStatus:
    """Config versus reality, with the mismatches named."""
    backend = resolve_backend()
    transport = configured_transport()
    host, port = configured_host(), configured_port()
    entry = _client_entry()

    st = DaemonStatus(
        backend=backend,
        running=False,
        configured_transport=transport,
        configured_host=host,
        configured_port=port,
        client_entry=entry,
        liveness_is_exact=_proc_fs_available(),
    )

    record = read_pidfile()
    if backend == "systemd":
        st.running = _systemd_unit_active()
        st.live_transport = _unit_transport()
        # Host and port stay unknown here: the unit bakes only the transport, and
        # the daemon reads the rest from .env at import. A port change is still
        # caught — the configured port stops answering — but a host change that
        # leaves the daemon reachable anyway (a wide bind) is not detectable.
        st.endpoint_check_is_exact = False
    else:
        st.running = pid_alive(record.get("pid"))
        if st.running:
            st.pid = int(record["pid"])
            st.live_transport = str(record.get("transport", ""))
            st.live_host = str(record.get("host", ""))
            live_port = record.get("port")
            st.live_port = int(live_port) if isinstance(live_port, int) else None

    st.port_open = port_open(host, port, timeout=0.5)

    if transport not in NETWORK_TRANSPORTS:
        if st.running:
            st.divergences.append(f"transport is {transport!r} but a daemon is running — nothing points at it")
        return st

    if st.running:
        if st.live_transport and st.live_transport != transport:
            st.divergences.append(f"daemon serves {st.live_transport!r}, config says {transport!r}")
        if st.live_port is not None and st.live_port != port:
            st.divergences.append(f"daemon on port {st.live_port}, config says {port}")
        if st.live_host and st.live_host != host:
            st.divergences.append(f"daemon bound {st.live_host!r}, config says {host!r}")
        if not st.port_open:
            st.divergences.append(f"{host}:{port} not answering despite a live process")

    if entry:
        expected_type = "sse" if transport == "sse" else "http"
        if "command" in entry and not entry.get("url"):
            # A stdio entry has no `type` at all in the older shape, so reporting
            # "type None" would send the reader looking for a typo instead of at
            # the real problem: the client is still told to spawn the server.
            st.divergences.append("~/.claude.json still holds a stdio command entry — run `agentihooks init`")
        elif entry.get("type") != expected_type:
            st.divergences.append(f"~/.claude.json declares type {entry.get('type')!r}, expected {expected_type!r}")
        url = str(entry.get("url", ""))
        if url and f":{port}" not in url:
            st.divergences.append(f"~/.claude.json url {url!r} does not name port {port}")
    elif transport in NETWORK_TRANSPORTS:
        st.divergences.append("~/.claude.json has no agentihooks MCP entry — run `agentihooks init`")

    return st


def _unit_transport() -> str:
    """The transport baked into the installed unit, or "" if unreadable."""
    unit = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    try:
        text = unit.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Environment=MCP_TRANSPORT="):
            return line.split("=", 2)[2].strip()
    return ""


def format_status(st: DaemonStatus) -> str:
    lines = [
        "agentihooks MCP daemon",
        f"  configured transport : {st.configured_transport}",
        f"  configured endpoint  : {st.configured_host}:{st.configured_port}",
    ]
    if st.client_entry:
        lines.append(f"  ~/.claude.json       : {st.client_entry.get('type')} {st.client_entry.get('url')}")
    else:
        lines.append("  ~/.claude.json       : no agentihooks MCP entry")
    lines += [
        f"  supervisor           : {st.backend}",
        f"  process              : {'running' if st.running else 'stopped'}" + (f" (pid {st.pid})" if st.pid else ""),
        f"  port                 : {'open' if st.port_open else 'closed'}",
    ]
    if not st.liveness_is_exact:
        lines.append("  [--] no /proc on this platform — a recycled pid could read as running")
    if not st.endpoint_check_is_exact:
        lines.append("  [--] under systemd the live host/port are not readable — only the configured port is probed")
    if st.divergences:
        lines.append("  DIVERGED:")
        lines += [f"    - {d}" for d in st.divergences]
    return "\n".join(lines)


def main(action: str, python: str, cwd: str) -> int:
    """CLI entry. Returns the process exit code."""
    transport = configured_transport()

    if action == "status":
        st = status()
        print(format_status(st))
        return st.exit_code()

    if action == "stop":
        print("Stopped." if stop() else "Nothing running.")
        return 0

    if action in ("start", "restart"):
        if transport not in NETWORK_TRANSPORTS:
            print(
                f"MCP_TRANSPORT is {transport!r} — Claude Code spawns the agentihooks MCP server per session "
                "and there is no daemon to run.",
                file=sys.stderr,
            )
            return 1
        state = ensure_running(transport, python, cwd, restart=(action == "restart"))
        if state.running:
            print(f"Daemon running ({state.backend}, {state.transport}, {state.detail}).")
            return 0
        print(f"Failed to start ({state.backend}): {state.detail}", file=sys.stderr)
        return 1

    print(f"unknown action {action!r}", file=sys.stderr)
    return 2
