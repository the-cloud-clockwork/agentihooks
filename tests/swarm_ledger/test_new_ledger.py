import pytest

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


def test_a_task_keeps_its_profile_only_when_the_content_names_one():
    tasks = [{"title": "a", "phase": "p1", "profile": "engineer"}, {"title": "b", "phase": "p1", "profile": ""}]

    doc = new_ledger.build_doc({**CONTENT, "tasks": tasks})

    assert [task.get("profile") for task in doc["tasks"]] == ["engineer", None]
    assert [task["id"] for task in doc["tasks"]] == ["t1", "t2"]


def test_a_task_profile_must_be_a_string():
    assert new_ledger.check_types({**CONTENT, "tasks": [{"title": "a", "profile": 1}]}) == [
        "tasks[1].profile must be a string"
    ]


@pytest.mark.parametrize("field", ["title", "description", "phase", "lane", "profile"])
def test_every_task_text_field_must_be_a_string(field):
    refused = [f"tasks[1].{field} must be a string"] + (["tasks[1].lane must be eng or ci"] if field == "lane" else [])

    assert new_ledger.check_types({**CONTENT, "tasks": [{field: 1}]}) == refused


def test_a_built_task_carries_its_fields_and_open_defaults():
    tasks = [
        {"title": "a", "description": "d", "phase": "p1", "lane": "ci", "profile": "engineer"},
        {"title": "b"},
    ]

    doc = new_ledger.build_doc({**CONTENT, "tasks": tasks})

    opened = {"comments": [], "done": False, "state": "open", "claimed_by": "", "issue_url": "", "pr_url": ""}
    assert doc["tasks"] == [
        {"id": "t1", "title": "a", "description": "d", "phase": "p1", "lane": "ci", "profile": "engineer", **opened},
        {"id": "t2", "title": "b", "description": "", "phase": "", "lane": "eng", **opened},
    ]


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
