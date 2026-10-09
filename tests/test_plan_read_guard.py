import sqlite3
from pathlib import Path

import pytest

from hooks.context import plan_read_guard
from hooks.context.plan_read_guard import check
from scripts.swarm_ledger.repository.rows import TABLES
from scripts.swarm_ledger.repository.sqlite import DATABASE, read_document
from tests.swarm_ledger import legacy_page

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
    put(tmp_path, SLUG, doc)
    return tmp_path


def put(folder, slug, doc):
    legacy_page.store(folder, slug, doc)


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


def test_plan_read_allowed_for_a_task_without_plan_lines(ledger):
    assert check(bash("agentihooks plan read"), env(ledger, task="t2")) is None


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
    assert check(read(stored(ledger)), env(ledger, AGENTIHOOKS_SWARM="absent")) == plan_read_guard.refusal(None)
    put(ledger, SLUG, {"artifacts": [{"plan": True}]})
    assert check(read(stored(ledger)), env(ledger)) == plan_read_guard.refusal(None)


def test_a_rootless_other_ledger_is_skipped(ledger):
    put(ledger, "other", {"artifacts": [{"file": {"id": "d" * 64 + ".md"}, "plan": True}]})
    with sqlite3.connect(ledger / DATABASE) as connection:
        for table in TABLES:
            connection.execute(f"DELETE FROM {table} WHERE slug='other' AND path='[]'")
    assert check(read(stored(ledger), offset=45, limit=5), env(ledger)) is None


def test_ledger_without_plans_allows_media_reads(ledger):
    put(ledger, SLUG, {})
    assert check(read(stored(ledger)), env(ledger)) is None


def test_non_numeric_offset_refused(ledger):
    assert check(read(stored(ledger), offset="abc", limit=5), env(ledger))
    assert check(read(stored(ledger), offset=[40], limit=5), env(ledger))


def test_boolean_offset_or_limit_refused(ledger):
    assert check(read(stored(ledger), offset=True, limit=5), env(ledger))
    assert check(read(stored(ledger), offset=45, limit=True), env(ledger))


def test_errors_while_deciding_refuse(ledger):
    assert check(bash(f"cat ~nosuchuser/x {PLAN}"), env(ledger))
    assert check(read(["x", PLAN], offset=45, limit=5), env(ledger))


def test_ledger_root_by_variable_or_relative_name_refused(ledger, monkeypatch):
    monkeypatch.setenv("LEDGERS", str(ledger))
    assert check({"tool_name": "Grep", "tool_input": {"path": "$LEDGERS"}}, env(ledger))
    assert check(bash(f"cd {ledger.parent} && grep -rn x {ledger.name}"), env(ledger))


def test_other_ledger_plan_and_split_artifact_address_refused(ledger):
    other = "d" * 64 + ".md"
    put(ledger, "other", {"artifacts": [{"file": {"id": other}, "plan": True}]})
    put(ledger, "junk", {"artifacts": [{"plan": True}]})
    assert check(bash(f"cat {ledger}/other.media/{other}"), env(ledger))
    assert check(bash(f"curl -s http://127.0.0.1:8765/artifacts/{SLUG}/${{A}}${{B}}.md"), env(ledger))


def test_github_artifact_download_allowed_and_ledger_address_by_variable_refused(ledger):
    repo = "repos/the-cloud-clockwork/agentihooks"
    assert check(bash(f"gh api {repo}/actions/artifacts/$ID/zip > mutation.zip"), env(ledger)) is None
    assert (
        check(
            bash(f'curl -sL -H "Authorization: Bearer $T" https://api.github.com/{repo}/actions/artifacts/$ID/zip'),
            env(ledger),
        )
        is None
    )
    assert check(bash(f"curl -s $LEDGER/artifacts/{SLUG}/${{A}}.md"), env(ledger))
    assert check(bash(f"curl -s ${{LEDGER}}/artifacts/{SLUG}/$A"), env(ledger))
    assert check(bash(f'curl -s "$L"/artifacts/{SLUG}/$A.md'), env(ledger))
    assert check(bash(f"curl -s $(base)/artifacts/{SLUG}/$A"), env(ledger))
    assert check(bash(f"curl -s http://h:1/pre/artifacts/{SLUG}/$A"), env(ledger))
    assert check(bash(f"curl -s $L/art/actions/artifacts/ifacts/{SLUG}/$A"), env(ledger))
    assert check(bash("curl -sL https://github.com/o/r/actions/runs/1/artifacts/$ID"), env(ledger)) is None


def test_quoted_sed_path_in_range_allowed(ledger):
    assert check(bash(f"sed -n '35,65p' \"{stored(ledger)}\""), env(ledger)) is None


def test_range_edges_allowed(ledger):
    assert check(read(stored(ledger), limit=18), env(ledger, task="t3")) is None
    assert check(read(stored(ledger), offset=45, limit=1), env(ledger)) is None
    assert check(bash(f"sed -n 45,45p {stored(ledger)}"), env(ledger)) is None


def test_plan_id_inside_another_name_refused(ledger):
    assert check(read(stored(ledger) + ".bak", offset=45, limit=5), env(ledger))


def test_swarm_unset_allowed_and_default_ledger_under_home(ledger):
    unset = {k: v for k, v in env(ledger).items() if k != "AGENTIHOOKS_SWARM"}
    assert check(read(stored(ledger)), unset) is None
    home = Path.home()
    (home / "development-ledger").mkdir(parents=True)
    (ledger / DATABASE).rename(home / "development-ledger" / DATABASE)
    path = str(home / "development-ledger" / f"{SLUG}.media" / PLAN)
    assert check(read(path, offset=45, limit=5), {k: v for k, v in env(ledger).items() if k != "LEDGER_DIR"}) is None


def test_dollar_or_letters_alone_do_not_refuse(ledger):
    assert check(bash("echo $HOME"), env(ledger)) is None
    assert check(bash(f"ls {ledger}/XY"), env(ledger)) is None


def test_grep_of_ledger_root_with_a_glob_refused(ledger):
    assert check({"tool_name": "Grep", "tool_input": {"path": str(ledger), "glob": "*.md"}}, env(ledger))


def test_other_ledger_plans_add_to_this_one(ledger):
    put(ledger, "other", {"artifacts": [{"file": {"id": "d" * 64 + ".md"}, "plan": True}]})
    assert check(read(stored(ledger)), env(ledger))


def test_range_without_a_phase_plan_gets_no_window(ledger):
    doc = read_document(ledger, SLUG)
    doc["tasks"].append({"id": "t4", "phase": "p2", "plan_lines": "40-60"})
    put(ledger, SLUG, doc)
    assert check(read(stored(ledger, LOOSE)), env(ledger, task="t4")) is None
    assert "Your chunk" not in check(read(stored(ledger)), env(ledger, task="t4"))


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


def test_hook_manager_reports_the_tool_and_session(ledger, monkeypatch):
    from unittest.mock import patch

    from hooks import hook_manager
    from hooks.hook_manager import BlockAction

    for key, value in env(ledger).items():
        monkeypatch.setenv(key, value)
    with patch.object(hook_manager.otel, "emit_event") as emit, pytest.raises(BlockAction):
        hook_manager.on_pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": stored(ledger)}})
    emit.assert_called_with("agentihooks.guardrail.plan_read_blocked", {"session.id": "", "tool_name": "Read"})
    with patch.object(hook_manager.otel, "emit_event") as emit, pytest.raises(BlockAction):
        hook_manager._plan_read_guard({"session_id": "s1", **read(stored(ledger))}, "Read")
    emit.assert_called_with("agentihooks.guardrail.plan_read_blocked", {"session.id": "s1", "tool_name": "Read"})


def test_hook_manager_logs_a_guard_crash(monkeypatch):
    from unittest.mock import patch

    from hooks import hook_manager

    with (
        patch.object(plan_read_guard, "check", side_effect=RuntimeError("boom")),
        patch.object(hook_manager, "log") as log,
    ):
        assert hook_manager._plan_read_guard(read("x"), "Read") is None
    log.assert_called_once_with("plan_read_guard failed", {"error": "boom"})


def test_credential_guard_helper_blocks_rewrites_and_logs(monkeypatch):
    from unittest.mock import patch

    from hooks import hook_manager
    from hooks.context import credential_guard
    from hooks.hook_manager import BlockAction

    monkeypatch.setattr("hooks.config.CREDENTIAL_GUARD_ENABLED", True)
    verdict = credential_guard.Verdict
    with patch.object(credential_guard, "evaluate", return_value=verdict()):
        assert hook_manager._credential_guard(read("x"), "Read") is None
    with patch.object(credential_guard, "evaluate", return_value=verdict(rewrite={"a": 1})):
        assert hook_manager._credential_guard(read("x"), "Read") == ({"a": 1}, "")
    with patch.object(credential_guard, "evaluate", return_value=verdict(rewrite={"a": 1}, note="n")):
        assert hook_manager._credential_guard(read("x"), "Read") == ({"a": 1}, "n")
    with (
        patch.object(credential_guard, "evaluate", return_value=verdict(block="no")),
        patch.object(hook_manager.otel, "emit_event") as emit,
        pytest.raises(BlockAction, match="^no$"),
    ):
        hook_manager._credential_guard(read("x"), "Read")
    emit.assert_called_once_with(
        "agentihooks.guardrail.credential_read_blocked", {"session.id": "", "tool_name": "Read"}
    )
    with (
        patch.object(credential_guard, "evaluate", return_value=verdict(block="no")),
        patch.object(hook_manager.otel, "emit_event") as emit,
        pytest.raises(BlockAction),
    ):
        hook_manager.on_pre_tool_use({"session_id": "s1", **read("x")})
    emit.assert_called_once_with(
        "agentihooks.guardrail.credential_read_blocked", {"session.id": "s1", "tool_name": "Read"}
    )
    with (
        patch.object(credential_guard, "evaluate", side_effect=RuntimeError("boom")),
        patch.object(hook_manager, "log") as log,
    ):
        assert hook_manager._credential_guard(read("x"), "Read") is None
    log.assert_called_once_with("credential_guard failed", {"error": "boom"})
