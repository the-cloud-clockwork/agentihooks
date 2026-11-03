import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core  # noqa: E402


def derived_paths(path):
    return {
        "ledger_server": {"PIDFILE": path / ".server.pid", "LOGFILE": path / ".server.log"},
        "ledger_hook": {"LEDGER_DIR": path, "SESSIONS": path / ".sessions"},
    }


@pytest.fixture(scope="module", autouse=True)
def ledger_dir(tmp_path_factory):
    path = tmp_path_factory.mktemp("ledger")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LEDGER_DIR", str(path))
        patch.setattr(ledger_core, "LEDGER_DIR", path)
        for module, values in derived_paths(path).items():
            if module in sys.modules:
                for name, value in values.items():
                    patch.setattr(sys.modules[module], name, value)
        yield path


@pytest.fixture(autouse=True)
def relay_journal():
    from scripts.swarm_ledger import storage

    journal = storage.default()
    if journal.path.exists():
        for kind, slugs in journal.held_revisions().items():
            for slug in slugs:
                journal.drop_revisions(kind, slug)


@pytest.fixture(autouse=True)
def request_metrics_journal(monkeypatch: pytest.MonkeyPatch, _ledger_exit_guard: None) -> None:
    from collections import deque

    from scripts.swarm_ledger import request_metrics

    monkeypatch.setattr(request_metrics, "SAMPLES", deque(maxlen=request_metrics.MAX_SAMPLES))
    monkeypatch.setattr(request_metrics, "PENDING", [])
    monkeypatch.setattr(request_metrics, "EVICTED_THROUGH", float("-inf"))
    monkeypatch.setattr(request_metrics, "HELD_SINCE", None)
    monkeypatch.setattr(request_metrics, "LOST", [])
    monkeypatch.setattr(request_metrics, "JOURNAL", None)
    monkeypatch.setattr(request_metrics, "APPENDED", 0)
    monkeypatch.setattr(request_metrics, "STALE", False)


@pytest.fixture
def _ledger_exit_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = os.getpid()
    original_exit = os._exit

    def exit_process(code: int) -> None:
        if os.getpid() != owner:
            original_exit(code)
        raise AssertionError(f"ledger server exited the test process with status {code}")

    monkeypatch.setattr(os, "_exit", exit_process)
