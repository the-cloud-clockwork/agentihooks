from scripts.swarm_ledger import ledger_hook

SLUG = "autostart-2026-01-01"


def test_a_session_start_on_a_closed_port_starts_no_server_in_the_suite(tmp_path, monkeypatch, ledger_port):
    (tmp_path / f"{SLUG}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ledger_hook, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_PORT", str(ledger_port))
    seen = []
    monkeypatch.setattr(ledger_hook.subprocess, "Popen", lambda command, **kwargs: seen.append(command))
    ledger_hook.serve_ledgers()
    assert seen == []
