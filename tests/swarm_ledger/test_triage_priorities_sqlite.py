import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_server, new_ledger

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "profiles/package/skills/triage-priorities/scripts/list_priorities.py"
SLUG = "triage-sqlite-2026-01-01"


@pytest.fixture
def served(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    monkeypatch.setattr(ledger_server, "ALLOWED_HOSTS", {f"127.0.0.1:{httpd.server_port}"})
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd.server_port
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def cli_env(served, tmp_path):
    shim = tmp_path / "agentihooks"
    shim.write_text(f'#!/bin/sh\nshift\nexec "{sys.executable}" -m scripts.swarm_ledger.ledger "$@"\n')
    shim.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT),
        "LEDGER_PORT": str(served),
        "LEDGER_AUTOSTART": "0",
    }


def test_list_reads_open_priorities_from_a_sqlite_ledger(ledger_dir, cli_env):
    content = {
        "title": "Triage",
        "overview": "o",
        "phases": [{"title": "one"}],
        "questions": [{"text": "Which broker?"}],
    }
    new_ledger.create(SLUG, content)
    assert not (ledger_dir / f"{SLUG}.json").exists()
    done = subprocess.run(
        [sys.executable, str(SCRIPT), SLUG], env=cli_env, cwd=ROOT, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "open": [
            {
                "priority": "auto-questions-q1",
                "item": "questions/q1",
                "group": "questions",
                "ask": "Answer: Which broker?",
                "item_text": "Which broker?",
                "state": "",
                "recent": [],
            }
        ],
        "resolved": [],
    }
