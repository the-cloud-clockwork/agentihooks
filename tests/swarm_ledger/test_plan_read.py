import json

import pytest

from scripts.swarm_ledger import ledger_artifacts, plan_read
from scripts.swarm_ledger import ledger_core as core

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
    core.paths("chunks")[1].write_text(json.dumps(doc))
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
