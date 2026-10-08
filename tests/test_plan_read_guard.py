import json

import pytest

from hooks.context import plan_read_guard
from hooks.context.plan_read_guard import check

pytestmark = pytest.mark.unit

SLUG = "demo"
PLAN = "a" * 64 + ".md"
OTHER = "b" * 64 + ".md"
LOOSE = "c" * 64 + ".md"


@pytest.fixture
def ledger(tmp_path):
    doc = {
        "artifacts": [
            {"file": {"id": PLAN}, "plan": True},
            {"file": {"id": OTHER}, "plan": True},
            {"file": {"id": LOOSE}},
        ],
        "phases": [
            {"id": "p1", "plan_ref": {"artifact": f"http://127.0.0.1:8765/artifacts/{SLUG}/{PLAN}", "lines": "1-200"}},
            {"id": "p2"},
        ],
        "tasks": [
            {"id": "t1", "phase": "p1", "plan_lines": "40-60"},
            {"id": "t2", "phase": "p2"},
            {"id": "t3", "phase": "p1", "plan_lines": "5-8"},
        ],
    }
    (tmp_path / f"{SLUG}.json").write_text(json.dumps(doc))
    return tmp_path


def env(ledger, lane="eng", task="t1", **extra):
    return {
        "AGENTIHOOKS_SWARM": SLUG,
        "AGENTIHOOKS_SWARM_LANE": lane,
        "AGENTIHOOKS_SWARM_TASK": task,
        "LEDGER_DIR": str(ledger),
        **extra,
    }


def stored(ledger, name=PLAN):
    return str(ledger / f"{SLUG}.media" / name)


def read(path, **fields):
    return {"tool_name": "Read", "tool_input": {"file_path": path, **fields}}


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_whole_read_refused_and_names_the_command(ledger):
    reason = check(read(stored(ledger)), env(ledger))
    assert reason == (
        "BLOCKED: read plan artifacts through `agentihooks plan read`, which prints this task's chunk with "
        "ten lines of margin. Your chunk with its margin is lines 30 to 70; a Read with offset and limit or a "
        "`sed -n 'A,Bp'` inside them also passes."
    )


def test_in_range_read_allowed(ledger):
    assert check(read(stored(ledger), offset=30, limit=41), env(ledger)) is None


@pytest.mark.parametrize(("offset", "limit"), [(29, 10), (30, 42), (65, 10), (None, 41), (30, 0), (30, "10")])
def test_read_outside_range_refused(ledger, offset, limit):
    assert check(read(stored(ledger), offset=offset, limit=limit), env(ledger))


def test_margin_floors_at_line_one(ledger):
    assert check(read(stored(ledger), offset=1, limit=18), env(ledger, task="t3")) is None
    assert check(read(stored(ledger), limit=19), env(ledger, task="t3"))


def test_cat_of_stored_file_refused(ledger):
    assert check(bash(f"cat {stored(ledger)}"), env(ledger))


def test_sed_in_range_allowed_and_out_of_range_refused(ledger):
    assert check(bash(f"sed -n '30,70p' {stored(ledger)}"), env(ledger)) is None
    assert check(bash(f"sed -n 30,71p {stored(ledger)}"), env(ledger))
    assert check(bash(f"sed -n '45,40p' {stored(ledger)}"), env(ledger))
    assert check(bash(f"sed -n '30,70p' {stored(ledger)} {stored(ledger, OTHER)}"), env(ledger))


def test_media_folder_glob_and_search_refused(ledger):
    assert check(bash(f"cat {ledger}/{SLUG}.media/*.md"), env(ledger))
    assert check({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": f"{ledger}/{SLUG}.media"}}, env(ledger))


def test_grep_and_fetch_of_plan_refused(ledger):
    assert check({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": stored(ledger)}}, env(ledger))
    url = f"http://127.0.0.1:8765/artifacts/{SLUG}/{PLAN}"
    assert check({"tool_name": "WebFetch", "tool_input": {"url": url, "prompt": "p"}}, env(ledger))
    assert check(bash(f"curl -s {url}"), env(ledger))


def test_other_phase_plan_refused_even_in_range(ledger):
    assert check(read(stored(ledger, OTHER), offset=30, limit=10), env(ledger))


def test_task_without_range_gets_no_window(ledger):
    reason = check(read(stored(ledger), offset=1, limit=5), env(ledger, task="t2"))
    assert reason == plan_read_guard.refusal(None)
    assert "Your chunk" not in reason


@pytest.mark.parametrize("lane", ["master", "plan", ""])
def test_master_and_planner_lanes_allowed(ledger, lane):
    assert check(read(stored(ledger)), env(ledger, lane=lane)) is None


def test_ci_lane_refused(ledger):
    assert check(read(stored(ledger)), env(ledger, lane="ci"))


def test_outside_a_swarm_allowed(ledger):
    assert check(read(stored(ledger)), {**env(ledger), "AGENTIHOOKS_SWARM": ""}) is None


@pytest.mark.parametrize("value", ["false", "0", "off", " No "])
def test_off_switch(ledger, value):
    assert check(read(stored(ledger)), env(ledger, PLAN_READ_GUARD_ENABLED=value)) is None


def test_on_by_default_and_explicit_true(ledger):
    assert check(read(stored(ledger)), env(ledger, PLAN_READ_GUARD_ENABLED="true"))


def test_non_plan_artifact_and_other_files_allowed(ledger):
    assert check(read(stored(ledger, LOOSE)), env(ledger)) is None
    assert check(read("hooks/hook_manager.py"), env(ledger)) is None
    assert check(bash("agentihooks plan read"), env(ledger)) is None


def test_other_tools_and_bad_input_allowed(ledger):
    assert check({"tool_name": "Write", "tool_input": {"file_path": stored(ledger)}}, env(ledger)) is None
    assert check({"tool_name": "Read", "tool_input": stored(ledger)}, env(ledger)) is None


def test_missing_or_broken_ledger_refuses(ledger):
    assert check(read(stored(ledger)), env(ledger, AGENTIHOOKS_SWARM="absent"))
    (ledger / f"{SLUG}.json").write_text("{")
    assert check(read(stored(ledger)), env(ledger))
    (ledger / f"{SLUG}.json").write_text('{"artifacts": [{"plan": true}]}')
    assert check(read(stored(ledger)), env(ledger))


def test_ledger_without_plans_allows_media_reads(ledger):
    (ledger / f"{SLUG}.json").write_text("{}")
    assert check(read(stored(ledger)), env(ledger)) is None


def test_non_numeric_offset_refused(ledger):
    assert check(read(stored(ledger), offset="abc", limit=5), env(ledger))
    assert check(read(stored(ledger), offset=[40], limit=5), env(ledger))


@pytest.mark.parametrize(
    "command",
    [
        "cat {root}/de''mo.media/*",
        "cat {root}/*.media/*.md",
        "cat {root}/*/*.md",
        "grep -rn needle {root}",
        "grep -rn needle {root}/",
    ],
)
def test_spliced_globbed_and_recursive_reads_refused(ledger, command):
    assert check(bash(command.format(root=ledger)), env(ledger))


def test_ledger_root_search_refused_and_ledger_file_allowed(ledger):
    assert check({"tool_name": "Grep", "tool_input": {"pattern": "x", "path": str(ledger)}}, env(ledger))
    assert check(bash(f"cat {ledger}/{SLUG}.json"), env(ledger)) is None
    assert check(read(str(ledger / f"{SLUG}.json")), env(ledger)) is None


def test_reads_process_environment(ledger, monkeypatch):
    for key, value in env(ledger).items():
        monkeypatch.setenv(key, value)
    assert check(read(stored(ledger)))


def test_hook_manager_blocks_whole_read(ledger, monkeypatch):
    from hooks import hook_manager
    from hooks.hook_manager import BlockAction

    for key, value in env(ledger).items():
        monkeypatch.setenv(key, value)
    with pytest.raises(BlockAction, match="agentihooks plan read"):
        hook_manager.on_pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": stored(ledger)}})
