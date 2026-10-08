import json

import pytest

from scripts.swarm_ledger import ledger, ledger_artifacts, ledger_tasks, plan_ranges, plan_read
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import test_plan_kind, test_plan_ranges
from tests.swarm_ledger.test_plan_kind import add
from tests.swarm_ledger.test_plan_publish import cli, task

plan_ledger = test_plan_kind.plan_ledger
published = test_plan_ranges.published
LEGACY = "# Runtime\n## Phase BAL\n### bal6 — Capacity\nCapacity seam\n\n### bal7 — Display\nDisplay seam\n## Phase HIV\n### hiv2 — Daemon\nHive seam\n"


@pytest.fixture
def legacy(plan_ledger):
    file = ledger_artifacts.store(plan_ledger, "runtime.md", LEGACY.encode())
    url = f"{ledger.BASE}/artifacts/{plan_ledger}/{file['id']}"
    ops = [
        {"op": "join", "id": "join", "by": "planner", "role": "member"},
        {
            "op": "artifact_add",
            "id": "artifact",
            "by": "planner",
            "task": "",
            "title": "Plan",
            "file": file,
            "plan": True,
        },
        {"op": "phase_update", "id": "link", "by": "planner", "item": "phases/p1", "fields": {"plan_url": url}},
    ]
    for op in ops:
        core.check_op(op)
    assert core.sync(plan_ledger, ops=ops)[1] == []
    return plan_ledger


def test_task_set_computes_anchor_range(published, monkeypatch, capsys):
    add(published, "old")
    cli(monkeypatch, published, "task", "set", "old", "plan_slice=second")
    assert json.loads(capsys.readouterr().out) == {"task": "old", "plan_slice": "second", "plan_lines": "7-9"}
    row = task(core.sync(published)[0], "old")
    assert row["plan_slice"] == "second"
    assert row["plan_lines"] == "7-9"


def test_task_set_legacy_heading_and_task_add_share_the_resolver(legacy, monkeypatch):
    add(legacy, "old")
    cli(monkeypatch, legacy, "task", "set", "old", "plan_slice=bal6")
    cli(monkeypatch, legacy, "task", "add", "new", "New", "--phase", "p1", "--plan-slice", "hiv2")
    doc = core.sync(legacy)[0]
    assert task(doc, "old")["plan_lines"] == "3-4"
    assert task(doc, "new")["plan_lines"] == "9-10"
    assert plan_read.read(doc, legacy, "old", None) == LEGACY


def test_slice_heading_never_matches_a_prefix_or_fenced_example():
    text = "# Plan\n### bal60 — Other\nOther\n```md\n### bal6 — Example\n```\n### bal6 — Real\nReal\n### bal7 — Next\nNext\n"
    assert plan_ranges.slice_lines(text, "bal6", "1-10") == "7-8"


def test_slice_heading_is_unique_and_respects_phase_bounds():
    text = "# Plan\n## First\n### bal6 — One\nOne\n## Second\n### bal6 — Two\nTwo\n"
    assert plan_ranges.slice_lines(text, "bal6", "2-4") == "3-4"
    with pytest.raises(ValueError) as exc:
        plan_ranges.slice_lines(text, "bal6", "1-7")
    assert str(exc.value) == "slice anchor bal6 is missing or repeated in its phase"


def test_task_set_missing_slice_refuses_the_whole_update(published, monkeypatch):
    add(published, "old")
    with pytest.raises(SystemExit):
        cli(monkeypatch, published, "task", "set", "old", "plan_slice=absent", "description=Changed")
    row = task(core.sync(published)[0], "old")
    assert row["description"] == ""
    assert "plan_slice" not in row
    assert "plan_lines" not in row


def test_backfill_repairs_linked_tasks_and_lists_unresolved_headings(legacy, monkeypatch, capsys):
    for name in ("bal6", "hiv2", "absent", "closed", "existing", "unlinked"):
        add(legacy, name)
    cli(monkeypatch, legacy, "task", "set", "closed", "state=done")
    cli(monkeypatch, legacy, "task", "set", "existing", "plan_slice=bal7")
    cli(monkeypatch, legacy, "task", "set", "unlinked", "plan_url=")
    capsys.readouterr()
    cli(monkeypatch, legacy, "plan-backfill")
    assert json.loads(capsys.readouterr().out) == {
        "updated": ["bal6", "hiv2"],
        "missing": [{"task": "absent", "reason": "slice anchor absent is missing or repeated in its phase"}],
    }
    doc = core.sync(legacy)[0]
    assert task(doc, "bal6")["plan_lines"] == "3-4"
    assert task(doc, "hiv2")["plan_lines"] == "9-10"
    assert task(doc, "existing")["plan_slice"] == "bal7"
    assert task(doc, "existing")["plan_lines"] == "6-7"
    for name in ("closed", "absent", "unlinked"):
        assert "plan_lines" not in task(doc, name)
    cli(monkeypatch, legacy, "plan-backfill")
    assert json.loads(capsys.readouterr().out) == {
        "updated": [],
        "missing": [{"task": "absent", "reason": "slice anchor absent is missing or repeated in its phase"}],
    }


def test_backfill_uses_an_existing_slice_name(published, monkeypatch, capsys):
    add(published, "renamed")
    doc = core.sync(published)[0]
    task(doc, "renamed")["plan_slice"] = "third"
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: doc)
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append((kind, fields)))
    args = ledger.build_parser().parse_args(["--slug", published, "--as", "planner", "plan-backfill"])
    ledger.cmd_plan_backfill(args)
    assert sent == [
        (
            "task_update",
            {
                "item": "tasks/renamed",
                "fields": {"plan_slice": "third"},
                "if_state": ["open", "claimed", "blocked", "pr"],
            },
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"updated": ["renamed"], "missing": []}


def test_task_slice_field_is_validated_but_lines_remain_computed():
    op = {"op": "task_update", "by": "planner", "item": "tasks/old", "fields": {"plan_slice": "bal6"}}
    ledger_tasks.check(op)
    for fields in ({"plan_slice": 6}, {"plan_lines": "1-6"}):
        with pytest.raises(ValueError):
            ledger_tasks.check({**op, "fields": fields})


def test_slice_update_respects_missing_task_and_state_guard(published):
    add(published, "closed")
    for name, guard in (("missing", []), ("closed", ["claimed"])):
        op = {
            "op": "task_update",
            "id": name,
            "by": "planner",
            "item": f"tasks/{name}",
            "fields": {"plan_slice": "absent"},
            "if_state": guard,
        }
        doc, rejected = core.sync(published, ops=[op])
        assert rejected == ([name] if name == "missing" else [])
        assert doc["_meta"]["warnings"] == []
        assert "plan_lines" not in task(doc, "closed")


def test_backfill_continues_after_a_write_refusal(legacy, monkeypatch, capsys):
    for name in ("bal6", "hiv2"):
        add(legacy, name)
    doc = core.sync(legacy)[0]
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: doc)
    sent = []

    def send(args, kind, **fields):
        sent.append(fields["item"])
        if fields["item"] == "tasks/bal6":
            raise SystemExit("refused by ledger")

    monkeypatch.setattr(ledger, "send", send)
    args = ledger.build_parser().parse_args(["--slug", legacy, "--as", "planner", "plan-backfill"])
    ledger.cmd_plan_backfill(args)
    assert sent == ["tasks/bal6", "tasks/hiv2"]
    assert json.loads(capsys.readouterr().out) == {
        "updated": ["hiv2"],
        "missing": [{"task": "bal6", "reason": "refused by ledger"}],
    }
