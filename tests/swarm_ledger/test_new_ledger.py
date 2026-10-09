from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import new_ledger

SLUG = "upgrade-record"
CONTENT = {
    "title": "Upgrade <record>",
    "overview": "o",
    "sources": [],
    "phases": [{"title": "p"}],
    "questions": [],
    "followups": [],
}


def test_a_created_ledgers_shell_fills_every_placeholder(ledger_dir):
    new_ledger.create(SLUG, CONTENT)
    page = server.page_for(SLUG)
    assert "__LEDGER_" not in page
    assert "<title>Upgrade &lt;record&gt;</title>" in page


def test_creating_a_stored_slug_reports_it_exists_and_changes_nothing(ledger_dir, tmp_path, monkeypatch, capsys):
    import json
    import sys

    new_ledger.create(SLUG, CONTENT)
    before = new_ledger.repository.export_document(SLUG)
    content = tmp_path / "content.json"
    content.write_text(json.dumps(dict(CONTENT, title="Other")))
    monkeypatch.setattr(new_ledger, "built_slug", lambda args: SLUG)
    monkeypatch.setattr(sys, "argv", ["new_ledger", "--content", str(content)])
    new_ledger.main()
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f'{{"slug": "{SLUG}", "created": false}}'
    assert new_ledger.repository.export_document(SLUG) == before
