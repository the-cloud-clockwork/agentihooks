import json
import os
import socket
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from scripts.swarm import ledger_watchdog
from scripts.swarm_ledger import ledger_server, server_code, server_handoff


def module(path: Path) -> ModuleType:
    loaded = ModuleType(path.stem)
    loaded.__file__ = str(path)
    return loaded


def test_only_loaded_content_triggers_reload(tmp_path: Path) -> None:
    loaded = tmp_path / "loaded.py"
    unused = tmp_path / "unused.py"
    loaded.write_text("value = 1\n")
    unused.write_text("value = 1\n")
    modules = {"loaded": module(loaded)}
    code = server_code.LoadedCode(tmp_path, modules)
    original = loaded.stat().st_mtime_ns
    os.utime(loaded, ns=(original + 100, original + 100))
    unused.write_text("value = 2\n")
    assert not code.changed()
    loaded.write_text("value = 2\n")
    os.utime(loaded, ns=(original, original))
    assert code.changed()
    loaded.write_text("value = 1\n")
    assert not code.changed()


def test_lazy_modules_join_the_loaded_content_record(tmp_path: Path) -> None:
    modules: dict[str, ModuleType] = {}
    code = server_code.LoadedCode(tmp_path, modules)
    lazy = tmp_path / "lazy.py"
    lazy.write_text("value = 1\n")
    modules["lazy"] = module(lazy)
    assert not code.changed()
    server_code.record(tmp_path, 42, 0, (), files=code.files)
    assert not server_code.stale(tmp_path, 42)
    lazy.write_text("value = 2\n")
    assert code.changed()
    assert server_code.stale(tmp_path, 42)
    lazy.unlink()
    assert code.changed()
    assert server_code.stale(tmp_path, 42)


def test_foreign_and_unlocated_modules_are_not_server_code(tmp_path: Path) -> None:
    external = tmp_path / "external.py"
    external.write_text("value = 1\n")
    root = tmp_path / "repo"
    root.mkdir()
    code = server_code.LoadedCode(
        root, {"external": module(external), "builtin": ModuleType("builtin"), "missing": None}
    )
    assert code.files == {}
    assert not code.changed()


def test_server_close_finishes_active_requests_before_returning() -> None:
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def handle(request: socket.socket, address: tuple[str, int], server: object) -> None:
        started.set()
        assert release.wait(5)

    server = ledger_server.ThreadingHTTPServer(("127.0.0.1", 0), handle)
    request, peer = socket.socketpair()
    closer = threading.Thread(target=lambda: (server.server_close(), closed.set()))
    try:
        server.process_request(request, ("127.0.0.1", 1))
        assert started.wait(5)
        closer.start()
        assert not closed.wait(0.1)
    finally:
        release.set()
        closer.join(5)
        peer.close()
        server.server_close()
    assert closed.is_set()
    assert server.active == 0


def test_shared_successor_waits_until_its_predecessor_has_drained(monkeypatch: pytest.MonkeyPatch) -> None:
    parents = iter((42, 42, 42, 1))
    sleeps: list[float] = []
    monkeypatch.setattr(server_handoff.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(server_handoff.time, "sleep", sleeps.append)
    server_handoff.wait_predecessor()
    assert sleeps == [0.05, 0.05]


def test_watchdog_records_each_successful_code_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = iter(({"pid": 41, "stamp": 10, "digest": "old"}, {"pid": 42, "stamp": 20, "digest": "new"}))
    monkeypatch.setattr(ledger_watchdog.server_code, "loaded", lambda folder: next(snapshots))
    monkeypatch.setattr(ledger_watchdog, "relaunch", lambda host, command, flag: "")
    monkeypatch.setattr(ledger_watchdog.time, "time", lambda: 100.0)
    host = SimpleNamespace(folder=tmp_path)
    assert ledger_watchdog.reload_stale(host, ["server"]) == ""
    assert json.loads((tmp_path / ledger_watchdog.LOG).read_text()) == {
        "at": 100.0,
        "reload": "completed",
        "before": {"pid": 41, "stamp": 10, "digest": "old"},
        "after": {"pid": 42, "stamp": 20, "digest": "new"},
    }
