"""Tests for the agentihooks daemon lifecycle (scripts/mcp_daemon.py).

The behaviour under test is mostly about *not* being fooled: a recycled pid must
not read as a running daemon, a stale daemon must not pass as matching config, and
a machine holding state under both backends must be fully torn down.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import mcp_daemon  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Repoint every path the module writes, so nothing touches the real home."""
    d = tmp_path / "agentihooks"
    d.mkdir()
    # Paths resolve per call from AGENTIHOOKS_HOME / Path.home(), so redirecting
    # those two redirects every file the module touches — no module globals to
    # patch, and nothing left bound to the real home.
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(d))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return d


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AGENTIHOOKS_MCP_SUPERVISOR", "AGENTIHOOKS_MCP_TRANSPORT", "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT"):
        monkeypatch.delenv(var, raising=False)


class TestBackendSelection:
    def test_no_systemctl_binary_falls_back_to_pidfile(self, monkeypatch):
        monkeypatch.setattr(mcp_daemon.shutil, "which", lambda _: None)
        assert mcp_daemon.resolve_backend() == "pidfile"

    def test_no_user_bus_falls_back_to_pidfile(self, monkeypatch):
        """WSL2 without systemd=true: the binary exists, the bus does not.

        `systemctl --user is-active` cannot distinguish this from a stopped unit,
        which is why show-environment is the probe.
        """
        monkeypatch.setattr(mcp_daemon.shutil, "which", lambda _: "/usr/bin/systemctl")
        monkeypatch.setattr(
            mcp_daemon.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1, b"", b"Failed to connect to bus: No medium found"),
        )
        assert mcp_daemon.resolve_backend() == "pidfile"

    def test_working_user_bus_selects_systemd(self, monkeypatch):
        monkeypatch.setattr(mcp_daemon.shutil, "which", lambda _: "/usr/bin/systemctl")
        monkeypatch.setattr(
            mcp_daemon.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, b"LANG=C\n", b""),
        )
        assert mcp_daemon.resolve_backend() == "systemd"

    def test_typo_warns_and_falls_back_to_probing(self, monkeypatch, capsys):
        """Silently ignoring a typo means an operator who wrote `pid-file` to pin
        the fallback gets whatever probing decides, with no hint it did nothing.
        MCP_TRANSPORT already warns-and-falls-back; this now matches."""
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pid-file")
        monkeypatch.setattr(mcp_daemon.shutil, "which", lambda _: None)

        assert mcp_daemon.resolve_backend() == "pidfile"
        assert "unknown AGENTIHOOKS_MCP_SUPERVISOR" in capsys.readouterr().err

    def test_auto_does_not_warn(self, monkeypatch, capsys):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "auto")
        monkeypatch.setattr(mcp_daemon.shutil, "which", lambda _: None)

        mcp_daemon.resolve_backend()

        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("forced", ["systemd", "pidfile"])
    def test_env_override_wins_over_probing(self, monkeypatch, forced):
        def _explode(*_a, **_k):
            raise AssertionError("must not probe when the backend is forced")

        monkeypatch.setattr(mcp_daemon.shutil, "which", _explode)
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", forced)
        assert mcp_daemon.resolve_backend() == forced


class TestLiveness:
    def test_dead_pid_is_not_running(self, state_dir):
        assert mcp_daemon.pid_alive(999999) is False

    def test_no_pid_is_not_running(self, state_dir):
        assert mcp_daemon.pid_alive(None) is False
        assert mcp_daemon.pid_alive(0) is False

    def test_recycled_pid_is_not_mistaken_for_the_daemon(self, monkeypatch):
        """A live pid whose cmdline is some other program must read as stopped.

        Without the cmdline cross-check, `stop` would signal an unrelated process
        and `status` would report a daemon that does not exist.
        """
        monkeypatch.setattr(mcp_daemon.os, "kill", lambda *_a: None)
        monkeypatch.setattr(mcp_daemon, "_proc_fs_available", lambda: True)
        monkeypatch.setattr(mcp_daemon, "_read_cmdline", lambda _pid: b"/usr/bin/vim\x00notes.txt\x00")
        assert mcp_daemon.pid_alive(4242) is False

    @pytest.mark.parametrize(
        "cmdline",
        [
            b"/bin/grep\x00hooks.mcp\x00/var/log/x\x00",  # a grep FOR the marker
            b"/bin/cat\x00/tmp/hooks.mcp.log\x00",  # a path containing it
            b"/venv/bin/python\x00-m\x00hooks.mcp_reporter\x00",  # a sibling module
            b"/venv/bin/python\x00-c\x00hooks.mcp\x00",  # right word, wrong flag
        ],
    )
    def test_substring_lookalikes_are_not_the_daemon(self, monkeypatch, cmdline):
        """Only the adjacent `-m hooks.mcp` pair counts.

        A raw-substring test matches a log path; a bare whole-argv test still
        matches `grep hooks.mcp`. Both are things a recycled pid could land on.

        A recycled pid landing on any of these would make `stop` SIGTERM an
        unrelated process and `start` refuse to run because a phantom daemon
        appears live.
        """
        monkeypatch.setattr(mcp_daemon.os, "kill", lambda *_a: None)
        monkeypatch.setattr(mcp_daemon, "_proc_fs_available", lambda: True)
        monkeypatch.setattr(mcp_daemon, "_read_cmdline", lambda _pid: cmdline)
        assert mcp_daemon.pid_alive(4242) is False

    def test_matching_cmdline_is_running(self, monkeypatch):
        monkeypatch.setattr(mcp_daemon.os, "kill", lambda *_a: None)
        monkeypatch.setattr(mcp_daemon, "_proc_fs_available", lambda: True)
        monkeypatch.setattr(mcp_daemon, "_read_cmdline", lambda _pid: b"/venv/bin/python\x00-m\x00hooks.mcp\x00")
        assert mcp_daemon.pid_alive(4242) is True

    def test_without_procfs_the_signal_check_stands_alone(self, monkeypatch):
        monkeypatch.setattr(mcp_daemon.os, "kill", lambda *_a: None)
        monkeypatch.setattr(mcp_daemon, "_proc_fs_available", lambda: False)
        assert mcp_daemon.pid_alive(4242) is True


class TestPidfile:
    def test_round_trip(self, state_dir):
        mcp_daemon._write_pidfile({"pid": 123, "transport": "sse", "host": "localhost", "port": 8642})
        assert mcp_daemon.read_pidfile()["pid"] == 123

    def test_absent_pidfile_reads_as_empty(self, state_dir):
        assert mcp_daemon.read_pidfile() == {}

    def test_corrupt_pidfile_reads_as_empty(self, state_dir):
        mcp_daemon.pidfile().write_text("{not json", encoding="utf-8")
        assert mcp_daemon.read_pidfile() == {}

    def test_write_leaves_no_tmp_files_behind(self, state_dir):
        mcp_daemon._write_pidfile({"pid": 1})
        assert [p.name for p in state_dir.glob("*.tmp")] == []


class TestStop:
    def test_noop_with_neither_backend_present(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        assert mcp_daemon.stop() is False

    def test_stale_pidfile_is_cleared(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        mcp_daemon._write_pidfile({"pid": 999999})
        assert mcp_daemon.stop() is False
        assert not mcp_daemon.pidfile().exists()

    def test_tears_down_both_backends_not_just_the_detected_one(self, state_dir, monkeypatch):
        """A machine that had systemd and lost it can hold state in both places.

        Stopping only the detected backend leaves the other's process serving a
        port nothing points at.
        """
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")

        unit = Path.home() / ".config" / "systemd" / "user" / mcp_daemon.UNIT_NAME
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("[Service]\n", encoding="utf-8")

        calls = []
        monkeypatch.setattr(mcp_daemon, "_systemctl_usable", lambda: True)
        monkeypatch.setattr(
            mcp_daemon.subprocess,
            "run",
            lambda cmd, **k: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
        )

        killed = []
        mcp_daemon._write_pidfile({"pid": 4242})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: bool(pid) and int(pid) == 4242 and not killed)
        monkeypatch.setattr(mcp_daemon.os, "kill", lambda pid, sig: killed.append((pid, sig)))

        assert mcp_daemon.stop() is True
        assert killed and killed[0][0] == 4242
        assert any("stop" in c for c in calls), f"systemd stop never issued: {calls}"


class TestStatusDivergence:
    def _pidfile_backend(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")

    def test_port_mismatch_is_reported_and_exits_2(self, state_dir, monkeypatch):
        self._pidfile_backend(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        monkeypatch.setenv("MCP_PORT", "9000")
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "sse", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        st = mcp_daemon.status()

        assert st.running is True
        assert st.diverged is True
        assert any("8642" in d and "9000" in d for d in st.divergences), st.divergences
        assert st.exit_code() == 2

    def test_transport_mismatch_is_reported(self, state_dir, monkeypatch):
        """The exact fault hit live: .env said sse, the daemon served streamable-http."""
        self._pidfile_backend(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "streamable-http", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        st = mcp_daemon.status()

        assert any("streamable-http" in d for d in st.divergences), st.divergences
        assert st.exit_code() == 2

    def test_matching_daemon_exits_0(self, state_dir, monkeypatch):
        self._pidfile_backend(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        (Path.home() / ".claude.json").write_text(
            json.dumps({"mcpServers": {"agentihooks": {"type": "sse", "url": "http://localhost:8642/sse"}}}),
            encoding="utf-8",
        )
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "sse", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        st = mcp_daemon.status()

        assert st.divergences == []
        assert st.exit_code() == 0

    def test_stopped_daemon_exits_1(self, state_dir, monkeypatch):
        self._pidfile_backend(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        assert mcp_daemon.status().exit_code() == 1

    def test_stdio_config_with_a_running_daemon_is_divergence(self, state_dir, monkeypatch):
        """Reverting to stdio leaves a daemon nothing points at — say so."""
        self._pidfile_backend(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "sse", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        st = mcp_daemon.status()

        assert any("nothing points at it" in d for d in st.divergences), st.divergences

    def test_client_entry_type_mismatch_is_reported(self, state_dir, monkeypatch):
        """Claude Code names streamable-http "http"; an "sse" daemon behind an
        "http" entry never connects."""
        self._pidfile_backend(monkeypatch)
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        (Path.home() / ".claude.json").write_text(
            json.dumps({"mcpServers": {"agentihooks": {"type": "http", "url": "http://localhost:8642/mcp"}}}),
            encoding="utf-8",
        )
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "sse", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        assert any("declares type" in d for d in mcp_daemon.status().divergences)


class TestEnsureRunning:
    def test_stdio_stops_rather_than_starts(self, state_dir, monkeypatch):
        stopped = []
        monkeypatch.setattr(mcp_daemon, "stop", lambda: stopped.append(True) or True)

        state = mcp_daemon.ensure_running("stdio", "/venv/bin/python", "/repo")

        assert state.running is False
        assert state.backend == "none"
        assert stopped

    def test_start_on_a_live_daemon_does_not_spawn_a_second(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "sse", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)

        def _explode(*_a, **_k):
            raise AssertionError("spawned a second daemon racing for the port")

        monkeypatch.setattr(mcp_daemon, "_spawn_detached", _explode)

        state = mcp_daemon.ensure_running("sse", "/venv/bin/python", "/repo", restart=False)

        assert state.running is True
        assert state.detail == "already running"

    def test_restart_stops_first(self, state_dir, monkeypatch):
        order = []
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setattr(mcp_daemon, "_stop_unlocked", lambda: order.append("stop") or True)
        monkeypatch.setattr(mcp_daemon, "_spawn_detached", lambda *a, **k: order.append("spawn") or 777)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        mcp_daemon.ensure_running("sse", "/venv/bin/python", "/repo", restart=True)

        assert order == ["stop", "spawn"]

    def test_liveness_decision_happens_under_the_lock(self, state_dir, monkeypatch):
        """The start decision and the spawn must be one critical section.

        Checking liveness outside the lock let two concurrent starts both decide
        to spawn; the loser's bind failed on the taken port but it had already
        overwritten the pidfile with its own pid, so the pidfile named a corpse
        and the live daemon became unkillable through the CLI.
        """
        events = []

        import contextlib

        @contextlib.contextmanager
        def _tracking_lock():
            events.append("lock")
            yield
            events.append("unlock")

        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setattr(mcp_daemon, "_lock", _tracking_lock)
        monkeypatch.setattr(mcp_daemon, "_already_running", lambda: events.append("check") or False)
        monkeypatch.setattr(mcp_daemon, "_spawn_detached", lambda *a, **k: events.append("spawn") or 777)
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        mcp_daemon.ensure_running("sse", "/venv/bin/python", "/repo", restart=False)

        assert events == ["lock", "check", "spawn", "unlock"], events

    def test_a_dead_child_is_not_rescued_by_someone_elses_open_port(self, state_dir, monkeypatch):
        """An open port is not proof our child is serving it.

        If liveness were checked after the port, a daemon we failed to stop would
        make a corpse look like a successful start.
        """
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setattr(mcp_daemon, "_stop_unlocked", lambda: False)
        monkeypatch.setattr(mcp_daemon, "_spawn_detached", lambda *a, **k: 777)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)  # someone answers
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: False)  # but not us

        state = mcp_daemon.ensure_running("sse", "/venv/bin/python", "/repo")

        assert state.running is False
        assert "exited immediately" in state.detail

    def test_child_transport_is_explicit_not_inherited(self, state_dir, monkeypatch):
        """A stray `export MCP_TRANSPORT=` in the invoking shell must not reach the
        daemon — the same reason the unit bakes Environment= over EnvironmentFile=."""
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        env = mcp_daemon._child_env("sse")
        assert env["MCP_TRANSPORT"] == "sse"

    def test_daemon_that_exits_immediately_is_reported_as_failed(self, state_dir, monkeypatch):
        """A live pid is not evidence of a working daemon; the open port is."""
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setattr(mcp_daemon, "stop", lambda: False)
        monkeypatch.setattr(mcp_daemon, "_spawn_detached", lambda *a, **k: 777)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: False)
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: False)

        state = mcp_daemon.ensure_running("sse", "/venv/bin/python", "/repo")

        assert state.running is False
        assert "exited immediately" in state.detail
        assert not mcp_daemon.pidfile().exists()


class TestConfigResolution:
    def test_env_file_is_read_when_process_env_is_unset(self, state_dir):
        (state_dir / ".env").write_text("export MCP_TRANSPORT=sse  # inline\n", encoding="utf-8")
        assert mcp_daemon.configured_transport() == "sse"

    def test_first_assignment_wins_matching_hooks_config(self, state_dir):
        """hooks/config.py parses with os.environ.setdefault, so a later duplicate
        never overwrites an earlier one."""
        (state_dir / ".env").write_text("MCP_PORT=8642\nMCP_PORT=9999\n", encoding="utf-8")
        assert mcp_daemon.configured_port() == 8642

    def test_process_env_beats_the_file(self, state_dir, monkeypatch):
        (state_dir / ".env").write_text("MCP_TRANSPORT=sse\n", encoding="utf-8")
        monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
        assert mcp_daemon.configured_transport() == "streamable-http"

    def test_defaults_when_nothing_is_configured(self, state_dir):
        assert mcp_daemon.configured_transport() == "stdio"
        assert mcp_daemon.configured_host() == "localhost"
        assert mcp_daemon.configured_port() == 8642

    def test_non_integer_port_falls_back_rather_than_raising(self, state_dir):
        (state_dir / ".env").write_text("MCP_PORT=notanumber\n", encoding="utf-8")
        assert mcp_daemon.configured_port() == 8642


class TestEnvParserParity:
    """The daemon reads config through hooks/config.py; the tooling that manages
    it reads through mcp_daemon. Any behavioural gap between the two means the
    CLI refuses to manage a daemon that is in fact running, or manages it wrong.
    """

    def test_companion_env_file_is_read(self, state_dir):
        """hooks/config.py loads .env AND every other *.env in the directory.

        Reading only .env was the bug: a transport set in a companion file
        reached the daemon but not `agentihooks mcp start`, which then said
        "stdio — no daemon to run" about a network-mode server.
        """
        (state_dir / "custom.env").write_text("MCP_TRANSPORT=sse\n", encoding="utf-8")

        assert mcp_daemon.configured_transport() == "sse"

    def test_main_env_wins_over_a_companion(self, state_dir):
        """setdefault means first-assignment-wins, and .env is loaded first."""
        (state_dir / ".env").write_text("MCP_PORT=8642\n", encoding="utf-8")
        (state_dir / "aaa.env").write_text("MCP_PORT=9999\n", encoding="utf-8")

        assert mcp_daemon.configured_port() == 8642

    def test_companions_are_read_in_sorted_order(self, state_dir):
        (state_dir / "bbb.env").write_text("MCP_PORT=2222\n", encoding="utf-8")
        (state_dir / "aaa.env").write_text("MCP_PORT=1111\n", encoding="utf-8")

        assert mcp_daemon.configured_port() == 1111

    def test_file_order_matches_hooks_config(self, state_dir):
        """Pin the file list itself against the loader it mirrors."""
        for name in (".env", "zzz.env", "aaa.env"):
            (state_dir / name).write_text("", encoding="utf-8")

        names = [p.name for p in mcp_daemon._env_files()]

        assert names == [".env", "aaa.env", "zzz.env"]

    def test_value_parsing_matches_hooks_config(self, state_dir, monkeypatch):
        """Same quoting/export/comment handling as the canonical parser."""
        from hooks.config import _parse_env_file

        content = "export MCP_TRANSPORT='sse'  # quoted and exported\n"
        (state_dir / ".env").write_text(content, encoding="utf-8")

        saved = os.environ.copy()
        try:
            os.environ.pop("MCP_TRANSPORT", None)
            _parse_env_file(state_dir / ".env")
            canonical = os.environ.get("MCP_TRANSPORT")
        finally:
            os.environ.clear()
            os.environ.update(saved)

        assert mcp_daemon._scan_env_file("MCP_TRANSPORT") == canonical


class TestStatusFormatting:
    """`format_status` is what the operator actually reads; its output is
    documented in docs/hooks/mcp-transport.md and drifted from it once already."""

    def _diverged_status(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        monkeypatch.setenv("MCP_PORT", "9111")
        (Path.home() / ".claude.json").write_text(
            json.dumps({"mcpServers": {"agentihooks": {"type": "sse", "url": "http://localhost:8642/sse"}}}),
            encoding="utf-8",
        )
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "sse", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: False)
        return mcp_daemon.status()

    def test_every_divergence_is_printed(self, state_dir, monkeypatch):
        st = self._diverged_status(state_dir, monkeypatch)
        out = mcp_daemon.format_status(st)

        assert "DIVERGED:" in out
        for d in st.divergences:
            assert d in out, f"divergence dropped from the rendered output: {d}"

    def test_the_stale_client_url_is_among_them(self, state_dir, monkeypatch):
        """A url still naming the old port is its own divergence, distinct from
        the daemon's port — the doc's sample output omitted it."""
        st = self._diverged_status(state_dir, monkeypatch)

        assert any("does not name port 9111" in d for d in st.divergences), st.divergences

    def test_a_healthy_daemon_prints_no_diverged_block(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        (Path.home() / ".claude.json").write_text(
            json.dumps({"mcpServers": {"agentihooks": {"type": "sse", "url": "http://localhost:8642/sse"}}}),
            encoding="utf-8",
        )
        mcp_daemon._write_pidfile({"pid": 4242, "transport": "sse", "host": "localhost", "port": 8642})
        monkeypatch.setattr(mcp_daemon, "pid_alive", lambda pid: True)
        monkeypatch.setattr(mcp_daemon, "port_open", lambda *a, **k: True)

        assert "DIVERGED" not in mcp_daemon.format_status(mcp_daemon.status())


class TestCliMain:
    def test_start_refuses_under_stdio(self, state_dir, monkeypatch, capsys):
        monkeypatch.setenv("MCP_TRANSPORT", "stdio")
        assert mcp_daemon.main("start", "/venv/bin/python", "/repo") == 1
        assert "no daemon to run" in capsys.readouterr().err

    def test_status_returns_the_status_exit_code(self, state_dir, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_SUPERVISOR", "pidfile")
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        assert mcp_daemon.main("status", "/venv/bin/python", "/repo") == 1

    def test_unknown_action_exits_2(self, state_dir):
        assert mcp_daemon.main("frobnicate", "/venv/bin/python", "/repo") == 2


def test_module_does_not_import_hooks():
    """install.py deliberately does not depend on hooks.*; this is its surface."""
    source = (Path(__file__).parent.parent / "scripts" / "mcp_daemon.py").read_text(encoding="utf-8")
    assert "import hooks" not in source
    assert "from hooks" not in source


def test_pidfile_lives_where_clean_state_sweeps_it(state_dir):
    """_clean_state_dir globs "*.pid" in the state dir — the pidfile must be in
    scope of that sweep, and install.py must stop the daemon before it runs."""
    assert mcp_daemon.pidfile().suffix == ".pid"
    assert mcp_daemon.pidfile().parent == mcp_daemon.state_dir()


def test_os_environ_is_not_leaked_by_config_reads(state_dir):
    """Unlike hooks/config.py, this module's scan must not mutate os.environ —
    a leak here would poison the installer that imports it."""
    (state_dir / ".env").write_text("MCP_TRANSPORT=sse\n", encoding="utf-8")
    before = os.environ.copy()
    mcp_daemon.configured_transport()
    assert os.environ == before
