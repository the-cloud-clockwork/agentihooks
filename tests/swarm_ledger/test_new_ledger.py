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
