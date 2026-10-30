import importlib
import json
import os
import socket
import sys
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


def test_source_changed_after_import_before_capture_is_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "loaded_before_merge.py"
    path.write_text("value = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    code = server_code.LoadedCode(tmp_path)
    code.install()
    try:
        imported = importlib.import_module(path.stem)
        assert imported.value == 1
        path.write_text("value = 2\n")
        assert code.changed()
        server_code.record(tmp_path, 42, 0, (), files=code.files)
        assert server_code.stale(tmp_path, 42)
    finally:
        sys.meta_path.remove(code)
        sys.modules.pop(path.stem, None)


def test_startup_source_identity_matches_the_executing_code(tmp_path: Path) -> None:
    path = tmp_path / "start.py"
    path.write_text("value = 1\n")
    executing = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    assert server_code.source_identity(path, executing) == server_code.file_digest(path)
    path.write_text("value = 2\n")
    assert server_code.source_identity(path, executing) == ""


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
    parents = iter((42, 42, 1))
    sleeps: list[float] = []
    monkeypatch.setattr(server_handoff.os, "getppid", lambda: next(parents))
    monkeypatch.setattr(server_handoff.time, "sleep", sleeps.append)
    server_handoff.wait_predecessor(42)
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


def test_install_records_executing_sources_before_other_importers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "startup.py"
    path.write_text("value = 1\n")
    executing = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    code = server_code.LoadedCode(tmp_path, {})
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    code.install(executing)
    assert sys.meta_path[0] is code
    assert code.files[str(path.resolve())] == server_code.file_digest(path)
    assert code.files[str(Path(server_code.__file__).resolve())] == server_code.SELF_DIGEST
    path.write_text("value = 2\n")
    assert code.changed()


def test_record_digest_is_stable_across_file_order(tmp_path: Path) -> None:
    import hashlib

    files = {"z.py": "last", "a.py": "first"}
    server_code.record(tmp_path, 42, 7, (), files=files)
    first = json.loads((tmp_path / server_code.RECORD).read_text())
    expected = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    assert first["digest"] == expected
    server_code.record(tmp_path, 42, 7, (), files=dict(reversed(tuple(files.items()))))
    second = json.loads((tmp_path / server_code.RECORD).read_text())
    assert second["digest"] == expected


def test_source_loader_preserves_the_requested_module_name(tmp_path: Path) -> None:
    from unittest.mock import Mock

    loader = Mock()
    loader.get_filename.return_value = str(tmp_path / "module.py")
    source = server_code.Source(loader, {})
    assert source.get_filename("package.module") == str(tmp_path / "module.py")
    loader.get_filename.assert_called_once_with("package.module")


def test_finder_uses_package_paths_and_skips_non_source_loaders(tmp_path: Path) -> None:
    import importlib.machinery

    package = tmp_path / "package"
    package.mkdir()
    path = package / "child.py"
    path.write_text("value = 1\n")
    code = server_code.LoadedCode(tmp_path, {})
    spec = code.find_spec("package.child", [str(package)])
    assert spec is not None
    assert isinstance(spec.loader, server_code.Source)
    imported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imported)
    assert imported.value == 1
    assert code.find_spec("sys") is None
    (package / "namespace").mkdir()
    assert code.find_spec("package.namespace", [str(package)]) is None


def test_reload_uses_loaded_content_even_when_file_stamp_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    httpd = Mock()
    loaded = Mock()
    monkeypatch.setattr(ledger_server, "LOADED_CODE", loaded)
    monkeypatch.setattr(ledger_server, "code_stamp", lambda dirs: 7)
    monkeypatch.setattr(ledger_server, "code_digest", lambda dirs: "different")
    monkeypatch.setattr(ledger_server, "container_main", lambda: False)
    monkeypatch.setattr(ledger_server, "HANDOFF", {})
    loaded.changed.return_value = False
    handoff = Mock(return_value=b"{}")
    assert ledger_server.reload_if_changed(7, httpd, hand_off=handoff, loaded="running") == 7
    handoff.assert_not_called()
    loaded.changed.return_value = True
    assert ledger_server.reload_if_changed(7, httpd, hand_off=handoff, loaded="running") is None
    handoff.assert_called_once()
    httpd.shutdown.assert_called_once_with()


def test_refresh_persists_new_loaded_modules_and_leaves_unchanged_record_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    modules: dict[str, ModuleType] = {}
    code = server_code.LoadedCode(tmp_path, modules)
    monkeypatch.setattr(ledger_server, "LOADED_CODE", code)
    monkeypatch.setattr(ledger_server, "PIDFILE", tmp_path / "server.pid")
    monkeypatch.setattr(ledger_server, "LOADED_STAMP", 17)
    record = Mock(wraps=server_code.record)
    monkeypatch.setattr(server_code, "record", record)
    ledger_server.refresh_loaded_code()
    record.assert_not_called()
    path = tmp_path / "lazy.py"
    path.write_text("value = 1\n")
    modules["lazy"] = module(path)
    ledger_server.refresh_loaded_code()
    held = json.loads((tmp_path / server_code.RECORD).read_text())
    assert held["pid"] == os.getpid()
    assert held["stamp"] == 17
    assert held["files"] == {str(path.resolve()): server_code.file_digest(path)}
    record.assert_called_once()
    ledger_server.refresh_loaded_code()
    record.assert_called_once()


def test_watch_loop_passes_the_latest_stamp_and_server_to_handover(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    httpd = Mock()
    handover = Mock(side_effect=[11, None])
    monkeypatch.setattr(ledger_server, "reloading", lambda: True)
    monkeypatch.setattr(ledger_server, "LOADED_STAMP", 7)
    monkeypatch.setattr(ledger_server, "hand_over", handover)
    monkeypatch.setattr(ledger_server.ledger_bin, "tidy", lambda: None)
    monkeypatch.setattr(ledger_server, "bin_closed_without_swarm", lambda: None)
    monkeypatch.setattr(ledger_server, "unrelayed", lambda: False)
    monkeypatch.setattr(ledger_server, "refresh_loaded_code", lambda: None)
    monkeypatch.setattr(ledger_server, "sample_streams", lambda: None)
    monkeypatch.setattr(ledger_server, "metrics_journal", Mock())
    monkeypatch.setattr(ledger_server, "scheduled_wait", lambda interval: None)
    ledger_server.watch_ledgers(httpd)
    assert handover.call_args_list == [((7, httpd),), ((11, httpd),)]


def test_shared_successor_drains_the_original_parent_before_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    listener = object()
    monkeypatch.setattr(ledger_server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_server.os, "getppid", lambda: 424242)
    monkeypatch.setattr(server_handoff, "inherited", lambda: listener)
    monkeypatch.setattr(ledger_server, "warm_modules", lambda: None)
    monkeypatch.setattr(ledger_server, "warm", lambda: None)
    monkeypatch.setattr(ledger_server, "warm_streams", lambda: [])
    monkeypatch.setattr(ledger_server.stored, "generations", lambda: {})
    monkeypatch.setattr(server_handoff, "announce", Mock())
    monkeypatch.setattr(ledger_server.storage, "shared", lambda: True)
    wait = Mock(side_effect=SystemExit)
    monkeypatch.setattr(server_handoff, "wait_predecessor", wait)
    with pytest.raises(SystemExit):
        ledger_server.serve()
    wait.assert_called_once_with(424242)
