import sys
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_hook

SLUG = "autostart-2026-01-01"


@pytest.fixture
def starts(monkeypatch, ledger_port):
    monkeypatch.setattr(ledger, "repository", SimpleNamespace(exists=lambda slug: True, token=lambda slug: "t"))
    monkeypatch.setattr(ledger, "BASE", f"http://127.0.0.1:{ledger_port}")
    seen = []
    monkeypatch.setattr(ledger.subprocess, "run", lambda *args, **kwargs: seen.append((args, kwargs)))
    return seen


def test_a_failed_request_on_a_fixed_port_starts_no_server_in_the_suite(starts):
    with pytest.raises(SystemExit, match="ledger server not answering on http://127.0.0.1:"):
        ledger.call(SLUG)
    assert starts == []


def test_a_failed_request_outside_the_suite_still_starts_the_server(starts, monkeypatch):
    monkeypatch.delenv("LEDGER_AUTOSTART", raising=False)
    with pytest.raises(SystemExit, match="ledger server not answering"):
        ledger.call(SLUG)
    server = str(ledger.HERE / "ledger_server.py")
    assert starts == [(([sys.executable, server, "--ensure"],), {"check": False, "capture_output": True})]


def test_a_session_start_on_a_closed_port_starts_no_server_in_the_suite(tmp_path, monkeypatch, ledger_port):
    (tmp_path / f"{SLUG}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ledger_hook, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_PORT", str(ledger_port))
    seen = []
    monkeypatch.setattr(ledger_hook.subprocess, "Popen", lambda command, **kwargs: seen.append(command))
    ledger_hook.serve_ledgers()
    assert seen == []
