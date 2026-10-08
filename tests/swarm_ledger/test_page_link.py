import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger  # noqa: E402
import ledger_link  # noqa: E402
import new_ledger  # noqa: E402

CONTENT = {"title": "Link", "overview": "o", "phases": [{"title": "p", "description": "d"}]}


@pytest.fixture
def up(monkeypatch):
    monkeypatch.setenv("LEDGER_HOST", "10.0.0.5")
    monkeypatch.setenv("LEDGER_PORT", "9911")
    monkeypatch.setattr(ledger_link, "serving", lambda: str(ledger_link.folder()))


def test_the_link_comes_from_the_configured_host_port_and_slug(up):
    assert ledger_link.page_url("my-plan") == "http://10.0.0.5:9911/my-plan"
    assert ledger_link.page_line("my-plan") == (
        "Ledger page: http://10.0.0.5:9911/my-plan (open it to follow and steer the work)"
    )


def test_a_silent_server_is_named_with_the_command_that_starts_it(up, monkeypatch):
    monkeypatch.setattr(ledger_link, "serving", lambda: None)
    line = ledger_link.page_line("my-plan")
    assert line.startswith("Ledger page: http://10.0.0.5:9911/my-plan")
    assert "not answering" in line and ledger_link.START in line


def test_ledger_url_prints_the_line_without_an_agent_name(up, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", "my-plan", "url"])
    ledger.main()
    assert capsys.readouterr().out.strip() == ledger_link.page_line("my-plan")


def test_new_ledger_ends_with_the_page_line(up, monkeypatch, capsys, tmp_path):
    content = tmp_path / "content.json"
    content.write_text(json.dumps(CONTENT))
    monkeypatch.setattr(
        sys,
        "argv",
        ["new", "--content", str(content), "--plan", "link-plan.md", "--date", "2026-10-06", "--as", "worker"],
    )
    new_ledger.main()
    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[0])["created"] is True
    assert lines[-1] == ledger_link.page_line("link-plan-2026-10-06")
