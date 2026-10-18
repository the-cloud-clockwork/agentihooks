import io
import json
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_artifacts, ledger_tasks, plan_backfill, plan_packages, plan_read
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_backfill

legacy = test_plan_backfill.legacy
plan_ledger = test_plan_backfill.plan_ledger


def context():
    return SimpleNamespace(
        meta={}, refused=[], dirty=False, record=lambda *args, **kwargs: None, stamp=lambda *args: None
    )


@pytest.mark.parametrize("state,done", [("done", False), ("open", True)])
def test_backfill_respects_both_legacy_completion_fields(monkeypatch, capsys, state, done):
    doc = {"tasks": [{"id": "closed", "state": state, "done": done, "plan_url": "https://example.com/plan"}]}
    monkeypatch.setattr(ledger, "call", lambda slug: doc)
    monkeypatch.setattr(ledger, "send", lambda *args, **kwargs: pytest.fail("completed task must be preserved"))
    plan_backfill.run(SimpleNamespace(slug="proof"))
    assert json.loads(capsys.readouterr().out) == {"updated": [], "missing": []}


@pytest.mark.parametrize("doc", [{}, {"tasks": []}])
def test_backfill_of_an_empty_ledger_has_an_empty_report(monkeypatch, capsys, doc):
    monkeypatch.setattr(ledger, "call", lambda slug: doc)
    plan_backfill.run(SimpleNamespace(slug="proof"))
    assert json.loads(capsys.readouterr().out) == {"updated": [], "missing": []}


def test_slice_update_defaults_an_old_task_to_open_and_uses_its_own_link(legacy):
    doc = core.sync(legacy)[0]
    url = doc["phases"][0]["plan_url"]
    doc.pop("phases")
    row = {"id": "old", "plan_url": url}
    doc["tasks"] = [row]
    op = {
        "op": "task_update",
        "by": "planner",
        "item": "tasks/old",
        "fields": {"plan_slice": "bal6"},
        "if_state": ["open"],
    }
    assert ledger_tasks.apply(doc, op, context()) is True
    assert row["plan_lines"] == "3-4"


def test_guarded_slice_update_never_assigns_a_completed_task(legacy):
    doc = core.sync(legacy)[0]
    row = {"id": "old", "phase": "p1", "state": "done"}
    doc["tasks"] = [row]
    op = {
        "op": "task_update",
        "by": "planner",
        "item": "tasks/old",
        "fields": {"plan_slice": "absent"},
        "if_state": ["open"],
    }
    assert ledger_tasks.apply(doc, op, context()) is True
    assert row == {"id": "old", "phase": "p1", "state": "done"}


def test_slice_update_resolves_a_new_source_link_in_the_same_write(legacy):
    doc = core.sync(legacy)[0]
    url = doc["phases"][0]["plan_url"]
    doc["phases"] = []
    row = {"id": "old", "state": "open", "plan_url": "https://example.com/old"}
    doc["tasks"] = [row]
    op = {"op": "task_update", "by": "planner", "item": "tasks/old", "fields": {"plan_url": url, "plan_slice": "bal6"}}
    assert ledger_tasks.apply(doc, op, context()) is True
    assert row["plan_lines"] == "3-4"
    assert row["plan_url"] == url


def test_task_add_resolves_its_explicit_link_without_a_phase_plan(legacy):
    doc = core.sync(legacy)[0]
    url = doc["phases"][0]["plan_url"]
    doc["phases"] = []
    op = {
        "op": "task_add",
        "by": "planner",
        "task": "new",
        "title": "New",
        "lane": "eng",
        "plan_url": url,
        "plan_slice": "bal6",
    }
    assert ledger_tasks.apply(doc, op, context()) is True
    assert doc["tasks"][0]["plan_lines"] == "3-4"
    assert doc["tasks"][0]["plan_url"] == url


def test_task_add_without_any_plan_source_is_refused(plan_ledger):
    doc = core.sync(plan_ledger)[0]
    op = {
        "op": "task_add",
        "by": "planner",
        "task": "new",
        "title": "New",
        "lane": "eng",
        "phase": "p1",
        "plan_slice": "bal6",
    }
    ctx = context()
    assert ledger_tasks.apply(doc, op, ctx) is False
    assert ctx.refused == ["publish a plan artifact for the phase before adding a plan slice"]
    assert doc["tasks"] == []


def test_slice_update_without_any_source_preserves_the_task_and_names_the_refusal(plan_ledger):
    doc = core.sync(plan_ledger)[0]
    row = {"id": "old", "phase": "p1", "state": "open"}
    doc["tasks"] = [row]
    op = {"op": "task_update", "by": "planner", "item": "tasks/old", "fields": {"plan_slice": "bal6"}}
    ctx = context()
    assert ledger_tasks.apply(doc, op, ctx) is False
    assert ctx.refused == ["publish a plan artifact for the phase before adding a plan slice"]
    assert row == {"id": "old", "phase": "p1", "state": "open"}


def test_missing_only_update_is_an_accepted_noop_for_an_existing_range(legacy):
    doc = core.sync(legacy)[0]
    row = {"id": "old", "phase": "p1", "state": "open", "plan_slice": "bal6", "plan_lines": "3-4"}
    doc["tasks"] = [row]
    op = {
        "op": "task_update",
        "by": "planner",
        "item": "tasks/old",
        "fields": {"plan_slice": "absent"},
        "if_plan_lines_missing": True,
    }
    ctx = context()
    assert ledger_tasks.apply(doc, op, ctx) is True
    assert ctx.refused == []
    assert ctx.dirty is False
    assert row["plan_slice"] == "bal6"
    assert row["plan_lines"] == "3-4"


def test_phase_link_is_the_default_slice_source(legacy):
    from scripts.swarm_ledger import plan_ranges

    doc = core.sync(legacy)[0]
    assert plan_ranges.task_slice(doc, doc["phases"][0], "bal6") == "3-4"


def test_backfill_respects_the_task_phase_when_slice_names_repeat(legacy, monkeypatch, capsys):
    doc = core.sync(legacy)[0]
    source = "# Plan\n## One\n<!-- slice: work -->\n### Task\nOne\n## Two\n<!-- slice: work -->\n### Task\nTwo\n"
    file = ledger_artifacts.store(legacy, "phases.md", source.encode())
    url = f"{ledger.BASE}/artifacts/{legacy}/{file['id']}"
    doc["artifacts"].append({"plan": True, "file": file})
    doc["phases"] = [
        {"id": "p1", "plan_ref": {"artifact": url, "lines": "2-5"}},
        {"id": "p2", "plan_ref": {"artifact": url, "lines": "6-9"}},
    ]
    doc["tasks"] = [{"id": "first", "phase": "p1", "plan_slice": "work", "plan_url": url}]
    monkeypatch.setattr(ledger, "call", lambda slug: doc)

    def send(args, kind, **fields):
        assert ledger_tasks.apply(doc, {"op": kind, "by": "planner", **fields}, context()) is True
        return doc

    monkeypatch.setattr(ledger, "send", send)
    plan_backfill.run(SimpleNamespace(slug=legacy))
    assert json.loads(capsys.readouterr().out) == {"updated": ["first"], "missing": []}
    assert doc["tasks"][0]["plan_lines"] == "3-5"


def test_backfill_reads_a_task_link_without_a_phase(legacy, monkeypatch, capsys):
    doc = core.sync(legacy)[0]
    url = doc["phases"][0]["plan_url"]
    doc.pop("phases")
    doc["tasks"] = [{"id": "bal6", "plan_url": url}]
    monkeypatch.setattr(ledger, "call", lambda slug: doc)

    def send(args, kind, **fields):
        assert ledger_tasks.apply(doc, {"op": kind, "by": "planner", **fields}, context()) is True
        return doc

    monkeypatch.setattr(ledger, "send", send)
    plan_backfill.run(SimpleNamespace(slug=legacy))
    assert json.loads(capsys.readouterr().out) == {"updated": ["bal6"], "missing": []}
    assert doc["tasks"][0]["plan_lines"] == "3-4"


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"tasks": []},
        {"tasks": [{"id": "bal6", "plan_lines": "3-4", "plan_slice": "wrong"}]},
        {"tasks": [{"id": "bal6", "plan_lines": "6-7", "plan_slice": "bal6"}]},
    ],
)
def test_backfill_only_reports_a_confirmed_slice_and_range(legacy, monkeypatch, capsys, reply):
    doc = core.sync(legacy)[0]
    doc["tasks"] = [{"id": "bal6", "phase": "p1", "plan_url": doc["phases"][0]["plan_url"]}]
    monkeypatch.setattr(ledger, "call", lambda slug: doc)
    monkeypatch.setattr(ledger, "send", lambda *args, **kwargs: reply)
    plan_backfill.run(SimpleNamespace(slug=legacy))
    assert json.loads(capsys.readouterr().out) == {
        "updated": [],
        "missing": [{"task": "bal6", "reason": "task changed before its plan lines were saved"}],
    }


def test_plan_reader_uses_a_task_source_when_the_phase_has_no_link(legacy):
    doc = core.sync(legacy)[0]
    url = doc["phases"][0].pop("plan_url")
    doc["tasks"] = [{"id": "bal6", "phase": "p1", "plan_lines": "3-4", "plan_url": url}]
    assert plan_read.read(doc, legacy, "bal6", None) == test_plan_backfill.LEGACY


def test_repository_plan_is_read_as_utf8_under_another_default_encoding(tmp_path, monkeypatch):
    path = tmp_path / "plan.md"
    path.write_text("Accénts\n", encoding="utf-8")
    monkeypatch.setattr(plan_packages, "PLAN", path)
    original = io.text_encoding
    monkeypatch.setattr(
        io,
        "text_encoding",
        lambda encoding, stacklevel=2: "latin-1" if encoding is None else original(encoding, stacklevel),
    )
    assert plan_packages.text() == "Accénts\n"


def test_backfill_command_help_names_its_behavior():
    parser = ledger.build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    choice = next(a for a in action._choices_actions if a.dest == "plan-backfill")
    assert choice.help == "compute missing plan lines for linked unfinished tasks"
