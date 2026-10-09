import json
import re

import pytest

from scripts.swarm_ledger import ledger_artifacts, plan_read
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository

PLAN = "".join(f"line {n}\n" for n in range(1, 41))


def numbered(first, last):
    return "".join(f"line {n}\n" for n in range(first, last + 1))


@pytest.fixture
def slug(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    file = ledger_artifacts.store("chunks", "plan.md", PLAN.encode())
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/chunks/{file['id']}", "lines": "12-35"}
    doc = {
        "artifacts": [{"plan": True, "file": file}],
        "phases": [{"id": "p1", "plan_ref": ref}, {"id": "p2"}],
        "tasks": [
            {"id": "mid", "phase": "p1", "plan_lines": "15-17"},
            {"id": "top", "phase": "p1", "plan_lines": "2-3"},
            {"id": "bare", "phase": "p1"},
            {"id": "loose", "phase": "p2", "plan_lines": "4-5"},
        ],
    }
    SQLiteLedgerRepository(tmp_path / DATABASE).import_document("chunks", {**doc, "_meta": {"rev": 1}})
    return "chunks"


def run(capsys, argv, environ):
    assert plan_read.main(["read", *argv], environ) == 0
    return capsys.readouterr().out


def refusal(argv, environ):
    with pytest.raises(SystemExit) as caught:
        plan_read.main(["read", *argv], environ)
    return caught.value.code


def test_task_chunk_carries_ten_lines_of_margin_each_side(slug, capsys):
    assert run(capsys, ["--task", "mid"], {"AGENTIHOOKS_SWARM": slug}) == numbered(5, 27)


def test_margin_is_clamped_to_the_plan_start(slug, capsys):
    assert run(capsys, ["--task", "top"], {"AGENTIHOOKS_SWARM": slug}) == numbered(1, 13)


def test_task_and_swarm_default_to_the_session(slug, capsys):
    environ = {"AGENTIHOOKS_SWARM": slug, "AGENTIHOOKS_SWARM_TASK": "mid"}
    assert run(capsys, [], environ) == numbered(5, 27)


def test_slug_option_wins_over_the_session(slug, capsys):
    environ = {"AGENTIHOOKS_SWARM": "other", "AGENTIHOOKS_SWARM_TASK": "mid"}
    assert run(capsys, ["--slug", slug], environ) == numbered(5, 27)


def test_phase_option_prints_the_phase_range_clamped_to_the_plan_end(slug, capsys):
    environ = {"AGENTIHOOKS_SWARM": slug, "AGENTIHOOKS_SWARM_TASK": "mid"}
    assert run(capsys, ["--phase", "p1"], environ) == numbered(2, 40)


def test_chunk_keeps_the_plan_lines_verbatim():
    assert plan_read.chunk("a\nb\nc\n", "2-2") == "a\nb\nc\n"


def test_exact_is_the_range_without_margin_and_refuses_an_empty_one(slug):
    doc = SQLiteLedgerRepository(core.LEDGER_DIR / DATABASE).read(slug, "phases", "artifacts")
    ref = doc["phases"][0]["plan_ref"]
    assert plan_read.exact(doc, ref, "15-17") == numbered(15, 17)
    with pytest.raises(ValueError) as caught:
        plan_read.exact(doc, ref, "45-46")
    assert str(caught.value) == "plan lines 45-46 hold no text"


def test_numbered_keeps_the_plan_line_numbers_and_skips_blank_rows():
    assert plan_read.numbered(" a \n\n  \nd\n", "7-10") == [(7, "a"), (10, "d")]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--task", "bare"], "task bare has no plan lines"),
        (["--task", "gone"], "no task gone in ledger chunks"),
        (["--task", "loose"], "phase p2 has no plan range"),
        (["--phase", "p2"], "phase p2 has no plan range"),
        (["--phase", "p9"], "no phase p9 in ledger chunks"),
        ([], "plan read needs a task: pass --task or run it inside a swarm task session"),
    ],
)
def test_refusals_are_plain(slug, argv, message):
    assert refusal(argv, {"AGENTIHOOKS_SWARM": slug}) == message


def test_refused_without_a_swarm(slug):
    assert refusal(["--task", "mid"], {}) == "plan read needs a swarm: pass --slug or run it inside a swarm session"


def test_refused_for_a_missing_ledger(slug):
    assert refusal(["--task", "mid"], {"AGENTIHOOKS_SWARM": "nowhere"}) == "no ledger nowhere"


def test_refused_for_an_invalid_slug_or_a_ledger_file_that_is_not_stored(slug):
    assert refusal(["--task", "mid", "--slug", "../x"], {}) == "no ledger ../x"
    core.paths("filed")[1].write_text(json.dumps({"tasks": [{"id": "mid"}]}))
    assert refusal(["--task", "mid"], {"AGENTIHOOKS_SWARM": "filed"}) == "no ledger filed"


def test_pointer_names_the_command_only_for_a_sliced_task():
    assert plan_read.pointer({"plan_lines": "15-17", "phase": "p1"}) == (
        "Plan: run agentihooks plan read to read only your slice of the plan, lines 15-17 "
        "with ten lines of margin each side; add --phase p1 for the whole phase."
    )
    assert plan_read.pointer({"plan_url": "https://example.com/plan"}) == ""


def test_installer_delegates_plan_deps_and_quota():
    import scripts.agents_quota
    import scripts.deps_preflight
    from scripts.cli_delegates import delegated_cli

    assert delegated_cli(["plan", "read"]) is plan_read.main
    assert delegated_cli(["deps", "check"]) is scripts.deps_preflight.main
    assert delegated_cli(["quota"]) is scripts.agents_quota.main


def test_installer_help_lists_the_plan_command(monkeypatch, capsys):
    import sys

    from scripts import install

    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(sys, "argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit):
        install.main()
    help_text = capsys.readouterr().out
    assert re.search(r"^ +plan +Read only your task's plan chunk: read \[--task ID\] \[--phase ID\]$", help_text, re.M)


def test_help_names_the_command_and_its_options(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit):
        plan_read.main(["--help"], {})
    top = capsys.readouterr().out
    assert top.startswith("usage: agentihooks plan [-h] {read} ...\n")
    assert "agentihooks plan read: the task's plan chunk with ten lines of margin on each side." in top
    assert re.search(r"^ +read +print the task's plan chunk with ten lines of margin on each side$", top, re.M)
    with pytest.raises(SystemExit):
        plan_read.main(["read", "--help"], {})
    reader = capsys.readouterr().out
    for line in (
        r"--task TASK +task id; defaults to AGENTIHOOKS_SWARM_TASK",
        r"--phase PHASE +print this phase's whole plan range instead",
        r"--slug SLUG +ledger slug; defaults to AGENTIHOOKS_SWARM",
    ):
        assert re.search(rf"^ +{line}$", reader, re.M)


def test_a_subcommand_is_required(capsys):
    with pytest.raises(SystemExit) as caught:
        plan_read.main([], {})
    assert caught.value.code == 2
    assert capsys.readouterr().err.endswith("error: the following arguments are required: command\n")


def test_read_refuses_a_ledger_without_tasks_or_phases():
    with pytest.raises(ValueError) as task:
        plan_read.read({}, "bare", "mid", None)
    assert str(task.value) == "no task mid in ledger bare"
    with pytest.raises(ValueError) as phase:
        plan_read.read({}, "bare", None, "p1")
    assert str(phase.value) == "no phase p1 in ledger bare"


def test_ledger_modules_load_with_the_ledger_folder_first_on_the_path(monkeypatch):
    import sys

    from scripts.swarm_ledger import HERE

    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != str(HERE)])
    assert plan_read._ledger("plan_ranges").__name__ == "scripts.swarm_ledger.plan_ranges"
    assert sys.path[0] == str(HERE)
    plan_read._ledger("plan_ranges")
    assert sys.path.count(str(HERE)) == 1


LINKED_PLAN = """# Plan
## Work
### W1
- Positive: [A](#case.a).
- Negative: [B](#case-b), again [A](#case.a).
- Recovery: [C](#case-c), gone [X](#missing), bare [D](#no-heading).
### W2
- outside link [E](#case-e).
## Cases
<a id="case.a"></a>

### Case A
- a body
```
## fenced, not a heading
```
#### Detail
- a detail

<a id="case-b"></a>

### Case B
- b body
## Tail
<a id="case-c"></a>

#### Case C
- c body
### After
<a id="case-e"></a>
### Case E
- e body
<a id="no-heading"></a>
"""
CASE_A = "### Case A\n- a body\n```\n## fenced, not a heading\n```\n#### Detail\n- a detail\n"
CASE_B = "### Case B\n- b body\n"
CASE_C = "#### Case C\n- c body\n"


def test_linked_appends_each_anchored_section_once_in_link_order():
    assert plan_read.linked(LINKED_PLAN, "3-6") == f"\n{CASE_A}\n{CASE_B}\n{CASE_C}"


def test_linked_follows_only_links_inside_the_slice():
    assert plan_read.linked(LINKED_PLAN, "4-4") == f"\n{CASE_A}"
    assert plan_read.linked(LINKED_PLAN, "3-5") == f"\n{CASE_A}\n{CASE_B}"


def test_linked_skips_a_section_anchored_inside_the_slice():
    source = '## W\n- see [A](#ca) and [B](#cb)\n<a id="ca"></a>\n### A\n- a\n<a id="cb"></a>\n### B\n- b\n'
    assert plan_read.linked(source, "1-5") == "\n### B\n- b\n"


def test_linked_is_empty_for_a_slice_without_anchor_links():
    assert plan_read.linked(LINKED_PLAN, "1-2") == ""


def test_section_runs_to_the_plan_end_without_a_later_heading():
    assert plan_read.section('<a id="z"></a>\n## Z\n- z\n  \n<a id="y"></a>\n', "z") == "## Z\n- z\n"


def test_section_is_empty_unless_a_heading_follows_the_anchor():
    assert plan_read.section(LINKED_PLAN, "no-heading") == ""
    assert plan_read.section(LINKED_PLAN, "missing") == ""
    assert plan_read.section('<a id="x"></a>\n\nprose\n## Other\n- o\n', "x") == ""


def test_task_read_appends_the_linked_cases_after_the_slice(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    file = ledger_artifacts.store("linked", "plan.md", LINKED_PLAN.encode())
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/linked/{file['id']}", "lines": "1-33"}
    doc = {
        "artifacts": [{"plan": True, "file": file}],
        "phases": [{"id": "p1", "plan_ref": ref}],
        "tasks": [{"id": "w1", "phase": "p1", "plan_lines": "3-6"}, {"id": "head", "phase": "p1", "plan_lines": "1-1"}],
    }
    SQLiteLedgerRepository(tmp_path / DATABASE).import_document("linked", {**doc, "_meta": {"rev": 1}})
    rows = LINKED_PLAN.splitlines()
    margin = "".join(f"{row}\n" for row in rows[:16])
    assert run(capsys, ["--task", "w1"], {"AGENTIHOOKS_SWARM": "linked"}) == f"{margin}\n{CASE_A}\n{CASE_B}\n{CASE_C}"
    assert run(capsys, ["--task", "head"], {"AGENTIHOOKS_SWARM": "linked"}) == "".join(f"{row}\n" for row in rows[:11])
    assert run(capsys, ["--phase", "p1"], {"AGENTIHOOKS_SWARM": "linked"}) == LINKED_PLAN


def test_swarm_v2_package_read_appends_the_linked_cases(monkeypatch):
    from scripts.swarm_ledger import plan_packages

    plan = "## 1. One\n## 2. Two\n## 3. Three\n" + LINKED_PLAN.replace("# Plan\n", "")
    monkeypatch.setattr(plan_packages, "text", lambda: plan)
    assert plan_packages.read("5-8").endswith(f"\n{CASE_A}\n{CASE_B}\n{CASE_C}")
