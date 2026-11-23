"""A command line ledger write goes to this host's warm writer socket and loads neither the ledger API service, the
ledger server nor the database ledger adapter when the writer answers."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from hooks import config
from scripts.swarm_ledger import ledger, warm_writer
from scripts.tick.adapters import database_ledger
from tests.tick.test_database_ledger_writer_route import Answers, asked, writer

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]
WRITTEN = b'201\n{"data": {"answered": "writer"}}'
HEAVY = (
    "scripts.swarm_ledger.api_service",
    "scripts.swarm_ledger.ledger_server",
    "scripts.tick.adapters.database_ledger",
)
SLIM_WRITE = (
    "import json, sys\n"
    "from scripts.swarm_ledger import ledger\n"
    "ledger.BASE = 'in-process'\n"
    "reply = ledger.client('sw', 5.0).request('sw', 'operations', {'ops': []})\n"
    f"print(json.dumps([reply, [name for name in {HEAVY!r} if name in sys.modules]]))\n"
)


@pytest.fixture
def home(memory_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A short agentihooks home on tmpfs, so the writer socket path fits a unix socket address."""
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", memory_path)
    monkeypatch.chdir(memory_path)
    return memory_path


@pytest.mark.feature("ledger-slim-write-client")
def test_a_command_line_client_write_goes_to_the_host_writer_socket(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = Answers()
    monkeypatch.setattr(database_ledger, "answered", answers)
    thread, heard = writer(warm_writer.socket_path(), WRITTEN)
    with database_ledger.acting("eng@ab-1"):
        reply = ledger.client("sw", 5.0).request("sw", "operations", {"ops": []})
    thread.join(10.0)
    assert reply == {"data": {"answered": "writer"}}
    assert answers.calls == []
    worktree = {"X-Ledger-Worktree": quote(os.getcwd())}
    assert heard == [asked("sw", "operations", {"ops": []}, "eng@ab-1", worktree)]


@pytest.mark.feature("ledger-slim-write-client")
def test_a_fresh_interpreter_write_through_a_live_writer_leaves_the_api_service_and_ledger_server_unloaded(
    home: Path,
) -> None:
    thread, heard = writer(warm_writer.socket_path(), WRITTEN)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV_CORE_", "COVERAGE_"))}
    env["AGENTIHOOKS_HOME"] = str(home)
    root = ROOT.parent if ROOT.name == "mutants" else ROOT
    done = subprocess.run(
        [sys.executable, "-c", SLIM_WRITE], cwd=root, env=env, capture_output=True, text=True, check=False, timeout=60
    )
    thread.join(10.0)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[-1]) == [{"data": {"answered": "writer"}}, []]
    assert [json.loads(sent)["path"] for sent in heard] == ["operations"]
