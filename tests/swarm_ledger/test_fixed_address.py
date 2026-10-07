import subprocess
import sys
from pathlib import Path

from scripts.swarm_ledger import ledger_link

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"


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


def test_proof_links_keep_the_explicit_spare_port(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("LEDGER_PORT", "8883")
    assert ledger_link.page_url("proof") == "http://127.0.0.1:8883/proof"
