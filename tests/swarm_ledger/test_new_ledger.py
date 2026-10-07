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


def test_upgrading_a_record_fills_every_placeholder(ledger_dir):
    new_ledger.create(SLUG, CONTENT)
    new_ledger.upgrade_page(SLUG)
    page = new_ledger.repository.read_page(SLUG)
    assert "__LEDGER_" not in page
    assert "<title>Upgrade &lt;record&gt;</title>" in page
