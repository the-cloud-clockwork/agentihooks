from pathlib import Path

import pytest

from tests.ledger_load import gate, memory


@pytest.mark.parametrize("problems", [[], ["write latency breach"]])
def test_paired_load_uses_memory_storage_and_preserves_its_result_and_server_log(monkeypatch, tmp_path, problems):
    folders = []

    def run(folder):
        folders.append(folder)
        assert folder.is_relative_to(Path("/dev/shm"))
        (folder / "server.log").write_text("server measurement\n")
        return problems, 0.3

    def command():
        assert gate.run(tmp_path) == (problems, 0.3)
        return 17

    monkeypatch.setattr(gate, "stop_server", lambda process: None)
    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "main", command)
    assert memory.main() == 17
    assert (tmp_path / "server.log").read_text() == "server measurement\n"
    assert not folders[0].exists()


@pytest.mark.parametrize("log", ["server failed\n", None])
def test_paired_load_keeps_the_server_log_and_removes_storage_when_the_run_raises(monkeypatch, tmp_path, log):
    folders = []

    def run(folder):
        folders.append(folder)
        if log is not None:
            (folder / "server.log").write_text(log)
        raise RuntimeError("server exited")

    monkeypatch.setattr(gate, "stop_server", lambda process: None)
    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "main", lambda: gate.run(tmp_path))
    with pytest.raises(RuntimeError, match="server exited"):
        memory.main()
    if log is not None:
        assert (tmp_path / "server.log").read_text() == log
    assert not folders[0].exists()


def test_paired_load_preserves_both_logs_when_a_write_breach_repeats(monkeypatch, tmp_path):
    runs = []

    def run(folder):
        runs.append(folder)
        (folder / "server.log").write_text(f"measure {len(runs)}")
        return (["write p95 3.0s exceeds 2.0s"], 3.0) if len(runs) == 1 else ([], 0.2)

    monkeypatch.setattr(gate, "stop_server", lambda process: None)
    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "main", lambda: int(bool(gate.measured(tmp_path))))
    assert memory.main() == 0
    assert (tmp_path / "server.log").read_text() == "measure 1"
    assert (tmp_path / "repeat" / "server.log").read_text() == "measure 2"
    assert len(set(runs)) == 2
    assert all(not folder.exists() for folder in runs)
