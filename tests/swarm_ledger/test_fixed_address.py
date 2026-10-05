import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_link
import ledger_server as server


def test_shared_link_ignores_an_inherited_proof_port(monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(Path.home() / "development-ledger"))
    monkeypatch.setenv("LEDGER_PORT", "8883")
    assert ledger_link.page_url("rig-grade-swarm") == "http://127.0.0.1:8765/rig-grade-swarm"


def test_shared_server_ignores_an_inherited_proof_port(monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(Path.home() / "development-ledger"))
    monkeypatch.setenv("LEDGER_PORT", "8883")
    result = subprocess.run(
        [sys.executable, "-c", "import ledger_server; print(ledger_server.BASE)"],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "http://127.0.0.1:8765"


@pytest.mark.parametrize("command", ["serve", "ensure"])
def test_proof_folder_cannot_take_the_shared_port(command, monkeypatch, tmp_path):
    monkeypatch.setattr(server.core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(server, "PORT", 8765)
    monkeypatch.setattr(server, "ThreadingHTTPServer", Mock())
    monkeypatch.setattr(server.threading, "Thread", Mock())
    monkeypatch.setattr(server, "serving_dir", lambda: str(tmp_path))
    with pytest.raises(SystemExit, match="reserved"):
        getattr(server, command)()
    server.ThreadingHTTPServer.assert_not_called()
    server.threading.Thread.assert_not_called()


def test_proof_links_keep_the_explicit_spare_port(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("LEDGER_PORT", "8883")
    assert ledger_link.page_url("proof") == "http://127.0.0.1:8883/proof"
