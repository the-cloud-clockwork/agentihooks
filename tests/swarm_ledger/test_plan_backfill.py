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


def test_task_set_using_only_its_phase_link_is_readable(legacy, monkeypatch):
    add(legacy, "old")
    cli(monkeypatch, legacy, "task", "set", "old", "plan_url=")
    cli(monkeypatch, legacy, "task", "set", "old", "plan_slice=bal6")
    doc = core.sync(legacy)[0]
    assert task(doc, "old")["plan_lines"] == "3-4"
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


def test_legacy_heading_cannot_assign_an_empty_slice():
    with pytest.raises(ValueError) as exc:
        plan_ranges.slice_lines("# Phase\n## bal6: Do work\nOne\n", "", "2-3")
    assert str(exc.value) == "slice anchor  is missing or repeated in its phase"


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

    def send(args, kind, **fields):
        sent.append((kind, fields))
        task(doc, "renamed").update(plan_slice="third", plan_lines="10-12")
        return doc

    monkeypatch.setattr(ledger, "send", send)
    args = ledger.build_parser().parse_args(["--slug", published, "--as", "planner", "plan-backfill"])
    ledger.cmd_plan_backfill(args)
    assert sent == [
        (
            "task_update",
            {
                "item": "tasks/renamed",
                "fields": {"plan_slice": "third"},
                "if_state": ["open", "claimed", "blocked", "pr"],
                "if_plan_lines_missing": True,
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


def test_missing_plan_lines_guard_is_boolean():
    op = {"op": "task_update", "by": "planner", "item": "tasks/old", "fields": {"plan_slice": "bal6"}}
    ledger_tasks.check({**op, "if_plan_lines_missing": True})
    with pytest.raises(ValueError) as exc:
        ledger_tasks.check({**op, "if_plan_lines_missing": "yes"})
    assert str(exc.value) == "if_plan_lines_missing must be a boolean"


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
        task(doc, "hiv2").update(plan_slice="hiv2", plan_lines="9-10")
        return doc

    monkeypatch.setattr(ledger, "send", send)
    args = ledger.build_parser().parse_args(["--slug", legacy, "--as", "planner", "plan-backfill"])
    ledger.cmd_plan_backfill(args)
    assert sent == ["tasks/bal6", "tasks/hiv2"]
    assert json.loads(capsys.readouterr().out) == {
        "updated": ["hiv2"],
        "missing": [{"task": "bal6", "reason": "refused by ledger"}],
    }


def test_backfill_does_not_report_a_guarded_noop_as_updated(legacy, monkeypatch, capsys):
    add(legacy, "bal6")
    snapshot = core.sync(legacy)[0]
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: snapshot)
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: {"tasks": [{"id": "bal6", "state": "done"}]})
    args = ledger.build_parser().parse_args(["--slug", legacy, "--as", "planner", "plan-backfill"])
    ledger.cmd_plan_backfill(args)
    assert json.loads(capsys.readouterr().out) == {
        "updated": [],
        "missing": [{"task": "bal6", "reason": "task changed before its plan lines were saved"}],
    }


def test_backfill_maps_repository_packages_and_reads_shared_sections(plan_ledger, monkeypatch, capsys, tmp_path):
    from scripts.swarm_ledger import plan_packages

    path = tmp_path / "plan.md"
    text = "# Swarm v2\n## 1. Architecture\nShared one\n## 2. Baseline\nShared two\n## 3. Boundaries\nShared three\n## 4. Packages\n#### SV2-RUN-05: Runtime\nRuntime seam\n#### SV2-RUN-06: Other\nOther seam\n"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(plan_packages, "PLAN", path)
    add(
        plan_ledger,
        "vrun5",
        plan_url="https://github.com/org/repo/issues/1371",
        description="Swarm v2 work package SV2-RUN-05. Read section SV2-RUN-05 plus sections 1 to 3.",
    )
    cli(monkeypatch, plan_ledger, "plan-backfill")
    assert json.loads(capsys.readouterr().out) == {"updated": ["vrun5"], "missing": []}
    doc = core.sync(plan_ledger)[0]
    row = task(doc, "vrun5")
    assert row["plan_slice"] == "SV2-RUN-05"
    assert row["plan_lines"] == "9-10"
    output = plan_read.read(doc, plan_ledger, "vrun5", None)
    assert (
        output
        == "## 1. Architecture\nShared one\n## 2. Baseline\nShared two\n## 3. Boundaries\nShared three\n\n" + text
    )


def test_backfill_preserves_a_range_assigned_after_its_snapshot(published, monkeypatch, capsys):
    add(published, "first")
    snapshot = core.sync(published)[0]
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: snapshot)

    def send(args, kind, **fields):
        competing = {
            "op": "task_update",
            "id": "competing",
            "by": "planner",
            "item": "tasks/first",
            "fields": {"plan_slice": "second"},
        }
        core.sync(published, ops=[competing])
        op = {"op": kind, "id": "backfill", "by": "planner", **fields}
        return core.sync(published, ops=[op])[0]

    monkeypatch.setattr(ledger, "send", send)
    args = ledger.build_parser().parse_args(["--slug", published, "--as", "planner", "plan-backfill"])
    ledger.cmd_plan_backfill(args)
    row = task(core.sync(published)[0], "first")
    assert row["plan_slice"] == "second"
    assert row["plan_lines"] == "7-9"
    assert json.loads(capsys.readouterr().out) == {
        "updated": [],
        "missing": [{"task": "first", "reason": "task changed before its plan lines were saved"}],
    }


def test_package_names_and_shared_section_validation(tmp_path, monkeypatch):
    from scripts.swarm_ledger import plan_packages

    assert plan_packages.name({"id": "old", "description": "SV2-RUN-05 and SV2-RUN-05"}) == "SV2-RUN-05"
    assert plan_packages.name({"id": "old", "description": "SV2-RUN-05", "plan_slice": "explicit"}) == "explicit"
    assert plan_packages.name({"id": "old"}) == "old"
    with pytest.raises(ValueError) as exc:
        plan_packages.name({"id": "old", "description": "SV2-RUN-05 and SV2-RUN-06"})
    assert str(exc.value) == "task description names more than one Swarm v2 package"
    with pytest.raises(ValueError) as exc:
        plan_packages.shared_lines("# Plan\n## 3. Third\n## 2. Second\n## 1. First\n")
    assert str(exc.value) == "Swarm v2 plan needs ordered shared sections 1 to 3"
    path = tmp_path / "plan.md"
    path.write_text(
        "# Plan\n## 1. Shared\nAccénts\n## 2. Shared\nTwo\n## 3. Shared\nThree\n## 4. Tasks\n#### SV2-RUN-05: Task\nTask\n#### T-SV2-RUN-05-A: Test\nFixture\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(plan_packages, "PLAN", path)
    assert plan_packages.text() == path.read_text(encoding="utf-8")
    assert plan_ranges.task_slice({}, {}, "SV2-RUN-05", "https://github.com/org/repo/issues/1371") == "9-10"
    assert plan_packages.shared_lines(plan_packages.text()) == "2-7"


@pytest.mark.parametrize("description", ["SV2-RUN-050", "XSV2-RUN-05", "T-SV2-RUN-05-A"])
def test_package_identifiers_never_match_another_identifier(description):
    from scripts.swarm_ledger import plan_packages

    assert plan_packages.name({"id": "other", "description": description}) == "other"


def test_nonstring_slice_is_refused_instead_of_crashing(legacy):
    doc, rejected = add(legacy, "malformed", plan_slice=True)
    assert rejected == ["add-malformed"]
    assert doc["_meta"]["warnings"] == ["plan slice must be a string"]


def test_package_named_slice_keeps_its_published_artifact(legacy, monkeypatch):
    from scripts.swarm_ledger import plan_packages

    doc = core.sync(legacy)[0]
    file = ledger_artifacts.store(
        legacy, "package.md", b"# Plan\n<!-- slice: SV2-RUN-05 -->\n### Runtime\nArtifact seam\n"
    )
    doc["artifacts"].append({"plan": True, "file": file})
    phase = {
        "plan_url": "https://github.com/org/repo/issues/1",
        "plan_ref": {"artifact": f"{ledger.BASE}/artifacts/{legacy}/{file['id']}", "lines": "1-4"},
    }
    monkeypatch.setattr(plan_packages, "text", lambda: pytest.fail("repository fallback must not replace an artifact"))
    assert plan_ranges.task_slice(doc, phase, "SV2-RUN-05", phase["plan_url"]) == "2-4"


def test_package_read_keeps_ten_lines_of_context(tmp_path, monkeypatch):
    from scripts.swarm_ledger import plan_packages

    shared = "## 1. Shared\nOne\n## 2. Shared\nTwo\n## 3. Shared\nThree\n"
    before = "".join(f"Before {n}\n" for n in range(12))
    after = "".join(f"After {n}\n" for n in range(12))
    source = "# Plan\n" + shared + "## 4. Tasks\n" + before + "#### SV2-RUN-05: Task\nTask\n" + after
    path = tmp_path / "plan.md"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(plan_packages, "PLAN", path)
    output = plan_packages.read("21-22")
    expected = (
        shared
        + "\n"
        + "".join(f"Before {n}\n" for n in range(2, 12))
        + "#### SV2-RUN-05: Task\nTask\n"
        + "".join(f"After {n}\n" for n in range(10))
    )
    assert output == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/org/repo/issues/1371", True),
        ("https://github.com/org/repo/pull/1371", False),
        ("https://example.com/org/repo/issues/1371", False),
        ("https://github.com/org/repo/issues/1371/more", False),
    ],
)
def test_repository_package_fallback_only_accepts_issue_links(url, expected):
    from scripts.swarm_ledger import plan_packages

    assert plan_packages.linked("SV2-RUN-05", url) is expected
    assert plan_packages.linked("ordinary", url) is False
