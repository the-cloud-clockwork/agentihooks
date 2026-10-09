import time

import pytest

from scripts.swarm.health import activity

BOUND = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}


@pytest.mark.parametrize(
    "tool,tool_input,kind",
    [
        ("Bash", {"command": "agentihooks ledger watch sw --as sw-eng-1"}, "watch"),
        ("Bash", {"command": "gh pr checks 12 --json name,bucket"}, "watch"),
        ("Bash", {"command": "gh run watch 991"}, "watch"),
        ("Bash", {"command": "agentihooks swarm sw status --json"}, ""),
        ("Bash", {"command": "agentihooks swarm sw verdict over-monitoring/sw-master-1 resolved"}, ""),
        ("Bash", {"command": "agentihooks swarm sw status; sleep 5"}, ""),
        ("Bash", {"command": 'agentihooks ledger --slug sw --as sw-eng-1 comment phases/p1 "x"'}, "act"),
        ("Bash", {"command": 'agentihooks ledger --slug sw --as sw-eng-1 say "x"'}, "act"),
        ("Bash", {"command": "agentihooks ledger --slug sw --as sw-master-1 phase p1 done"}, "act"),
        ("Bash", {"command": 'agentihooks ledger --slug sw --as sw-master-1 task add g9 "t"'}, "act"),
        ("Bash", {"command": 'agentihooks ledger --slug sw --as sw-eng-1 followup add "x"'}, "act"),
        ("Bash", {"command": 'agentihooks swarm sw say "x" --to eng'}, "act"),
        ("Bash", {"command": 'agentihooks swarm sw learned "x"'}, "act"),
        ("Bash", {"command": "agentihooks swarm sw done --pr https://x/pull/1"}, "act"),
        ("Bash", {"command": "agentihooks swarm sw --as sw-eng-1 pr https://x/pull/1"}, "act"),
        ("Bash", {"command": 'agentihooks msg reply abc "x"'}, "act"),
        ("Bash", {"command": "agentihooks ledger --slug sw --as sw-eng-1 ack"}, ""),
        ("Monitor", {"command": "agentihooks ledger watch sw --as sw-master-1"}, "watch"),
        ("Bash", {"command": "sleep 30"}, "watch"),
        ("Monitor", {"command": "tail -f x"}, "watch"),
        ("Edit", {"file_path": "a.py"}, "act"),
        ("Write", {"file_path": "a.py"}, "act"),
        ("mcp__serena__replace_symbol_body", {}, "act"),
        ("Bash", {"command": "git commit -m x && gh pr checks 3"}, "act"),
        ("Bash", {"command": "gh pr merge 12 --squash"}, "act"),
        ("Bash", {"command": "git push -u origin x"}, "act"),
        ("Bash", {"command": "cat a.py"}, ""),
        ("Read", {"file_path": "a.py"}, ""),
    ],
)
def test_classify_separates_watching_from_acting(tool, tool_input, kind):
    assert activity.classify(tool, tool_input) == kind


def test_record_counts_only_swarm_bound_sessions(tmp_path):
    activity.record("Monitor", {}, BOUND, tmp_path)
    activity.record("Bash", {"command": "sleep 5"}, BOUND, tmp_path)
    activity.record("Edit", {}, BOUND, tmp_path)
    activity.record("Read", {}, BOUND, tmp_path)
    activity.record("Monitor", {}, {"AGENTIHOOKS_SWARM": "sw"}, tmp_path)
    activity.record("Monitor", {}, {**BOUND, "AGENTIHOOKS_AGENT_NAME": "sw-eng-2"}, tmp_path)
    activity.record("Monitor", {}, {**BOUND, "AGENTIHOOKS_SWARM": "other"}, tmp_path)
    assert activity.counts("sw", tmp_path) == {
        "sw-eng-1": {"watch": 2, "act": 1, "since": 0},
        "sw-eng-2": {"watch": 1, "act": 0, "since": 1},
    }


def test_an_unsafe_name_or_a_missing_folder_records_and_counts_nothing(tmp_path):
    activity.record("Monitor", {}, {**BOUND, "AGENTIHOOKS_AGENT_NAME": "../x"}, tmp_path)
    assert activity.counts("sw", tmp_path) == {}
    assert activity.counts("none", tmp_path / "missing") == {}


def test_the_pre_tool_hook_records_a_bound_session(monkeypatch, tmp_path):
    from hooks import hook_manager

    for key, value in BOUND.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.setattr(activity, "default_root", lambda: tmp_path)
    hook_manager.on_pre_tool_use(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "sleep 1"}, "cwd": "/"}
    )
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}


def test_a_torn_line_is_skipped_and_the_rest_still_counts(tmp_path):
    (tmp_path / "sw").mkdir()
    (tmp_path / "sw" / "sw-eng-1.jsonl").write_text('{"kind": "watch"}\n{"kind": "wa\n{"kind": "act"}\n')
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 1, "act": 1, "since": 0}}


def test_an_unbound_session_never_classifies(monkeypatch, tmp_path):
    def boom(*_):
        raise AssertionError("classified an unbound call")

    monkeypatch.setattr(activity, "classify", boom)
    activity.record("Bash", {"command": "sleep 1"}, {}, tmp_path)
    assert activity.counts("sw", tmp_path) == {}


WINDOW = activity.REARM_WINDOW_MS
REARM = {"command": "agentihooks ledger watch sw --as sw-master-1"}


def test_re_arming_a_ledger_watch_counts_one_watch_per_expiry_window(tmp_path):
    for minute in (0, 1, 2, 29, 30, 31, 61):
        activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=minute * 60_000)
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 3, "act": 0, "since": 3}}


def test_other_watches_count_every_call(tmp_path):
    for second in range(4):
        activity.record("Bash", {"command": "gh pr checks 3"}, BOUND, tmp_path, now_ms=second * 1000)
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 4, "act": 0, "since": 4}}


def test_a_marked_re_arm_is_tagged_and_left_out_of_the_count(tmp_path):
    activity.record("Bash", {"command": "gh pr checks 3"}, BOUND, tmp_path, now_ms=0)
    activity.mark_revived("sw", "sw-eng-1", tmp_path, now_ms=1000)
    activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=2000)
    assert activity.rows_of("sw", "sw-eng-1", tmp_path)[-1] == {
        "kind": "watch",
        "at": 2000,
        "rearm": True,
        "revived": True,
    }
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}


def test_a_mark_tags_one_re_arm_and_is_used_up(tmp_path):
    activity.mark_revived("sw", "sw-eng-1", tmp_path, now_ms=0)
    activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=1000)
    activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=WINDOW + 2000)
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}


def test_a_mark_tags_only_a_re_arm_row(tmp_path):
    activity.mark_revived("sw", "sw-eng-1", tmp_path, now_ms=0)
    activity.record("Bash", {"command": "gh pr checks 3"}, BOUND, tmp_path, now_ms=1000)
    activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=2000)
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}


def test_a_stale_mark_tags_nothing(tmp_path):
    activity.mark_revived("sw", "sw-eng-1", tmp_path, now_ms=0)
    activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=activity.REVIVE_MARK_MS + 1)
    assert "revived" not in activity.rows_of("sw", "sw-eng-1", tmp_path)[-1]
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}


def test_a_mark_at_the_expiry_limit_still_tags(tmp_path):
    activity.mark_revived("sw", "sw-eng-1", tmp_path, now_ms=0)
    activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=activity.REVIVE_MARK_MS)
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 0, "act": 0, "since": 0}}


def test_a_mark_written_now_tags_a_re_arm_recorded_now(tmp_path):
    activity.mark_revived("sw", "sw-eng-1", tmp_path)
    activity.record("Monitor", REARM, BOUND, tmp_path)
    assert activity.rows_of("sw", "sw-eng-1", tmp_path)[-1]["revived"] is True


def test_a_mark_written_now_is_stale_past_the_expiry_limit(tmp_path):
    activity.mark_revived("sw", "sw-eng-1", tmp_path)
    later = int(time.time() * 1000) + activity.REVIVE_MARK_MS + 1000
    activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=later)
    assert "revived" not in activity.rows_of("sw", "sw-eng-1", tmp_path)[-1]


def test_a_mark_creates_missing_folders_and_a_later_mark_replaces_it(tmp_path):
    root = tmp_path / "fresh" / "activity"
    activity.mark_revived("sw", "sw-eng-1", root, now_ms=0)
    activity.mark_revived("sw", "sw-eng-1", root, now_ms=10 * activity.REVIVE_MARK_MS)
    activity.record("Monitor", REARM, BOUND, root, now_ms=10 * activity.REVIVE_MARK_MS + 1)
    assert activity.counts("sw", root) == {"sw-eng-1": {"watch": 0, "act": 0, "since": 0}}


def test_a_mark_for_an_unsafe_name_writes_nothing(tmp_path):
    activity.mark_revived("../sw", "sw-eng-1", tmp_path, now_ms=0)
    activity.mark_revived("sw", "x/../y", tmp_path, now_ms=0)
    assert not (tmp_path.parent / "sw").exists()
    assert not (tmp_path / "sw").exists()


def test_entries_keep_each_agents_timed_rows_and_tally_counts_them_as_counts_does(tmp_path):
    for minute in (0, 1, 31):
        activity.record("Monitor", REARM, BOUND, tmp_path, now_ms=minute * 60_000)
    activity.record("Edit", {"file_path": "a.py"}, BOUND, tmp_path, now_ms=32 * 60_000)
    rows = activity.entries("sw", tmp_path)
    assert [row["at"] for row in rows["sw-eng-1"]] == [0, 60_000, 31 * 60_000, 32 * 60_000]
    assert activity.tally(rows["sw-eng-1"]) == {"watch": 2, "act": 1}
    assert activity.counts("sw", tmp_path)["sw-eng-1"] == {"watch": 2, "act": 1, "since": 0}
    assert activity.tally(rows["sw-eng-1"][1:]) == {"watch": 2, "act": 1}
    assert activity.entries("missing", tmp_path) == {}


def test_an_agent_named_with_its_seat_code_is_recorded(tmp_path):
    named = {**BOUND, "AGENTIHOOKS_AGENT_NAME": "engineer@323133-0101"}
    activity.record("Bash", {"command": "gh pr checks 3"}, named, tmp_path, now_ms=0)
    assert activity.counts("sw", tmp_path) == {"engineer@323133-0101": {"watch": 1, "act": 0, "since": 1}}
    activity.record("Bash", {"command": "gh pr checks 3"}, {**named, "AGENTIHOOKS_AGENT_NAME": "a/../b"}, tmp_path)
    assert list(activity.counts("sw", tmp_path)) == ["engineer@323133-0101"]


def test_a_master_doing_ledger_writes_and_watches_does_not_trip_over_monitoring(tmp_path):
    from scripts.swarm.health import findings as health

    master = {**BOUND, "AGENTIHOOKS_AGENT_NAME": "sw-master-1"}
    at = 0
    for hour in range(8):
        for minute in range(0, 60, 2):
            at = (hour * 60 + minute) * 60_000
            activity.record("Monitor", REARM, master, tmp_path, now_ms=at)
            activity.record("Bash", {"command": "agentihooks swarm sw status"}, master, tmp_path, now_ms=at)
        activity.record(
            "Bash", {"command": 'agentihooks swarm sw say "progress" --to operator'}, master, tmp_path, now_ms=at
        )
        activity.record("Bash", {"command": 'agentihooks msg reply m1 "done"'}, master, tmp_path, now_ms=at)
    counts = activity.counts("sw", tmp_path)
    assert counts == {"sw-master-1": {"watch": 16, "act": 16, "since": 0}}
    assert health.over_monitoring(counts, health.Limits()) == []


def test_the_first_hook_event_of_each_agent_is_kept_whatever_the_tool(tmp_path):
    activity.record("Read", {"file_path": "a.py"}, BOUND, tmp_path, now_ms=5_000)
    activity.record("Edit", {}, BOUND, tmp_path, now_ms=9_000)
    activity.record("Read", {}, {**BOUND, "AGENTIHOOKS_AGENT_NAME": "sw-eng-2"}, tmp_path, now_ms=7_000)
    assert activity.first_events("sw", tmp_path) == {"sw-eng-1": 5_000, "sw-eng-2": 7_000}
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 0, "act": 1, "since": 0}}
    assert activity.first_events("none", tmp_path / "missing") == {}


def test_latest_tool_event_includes_unclassified_calls(tmp_path):
    activity.record("Edit", {}, BOUND, tmp_path, now_ms=5000)
    activity.record("Read", {}, BOUND, tmp_path, now_ms=9000)
    assert activity.last_events("sw", tmp_path) == {"sw-eng-1": 9000}
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 0, "act": 1, "since": 0}}
    assert activity.last_events("missing", tmp_path) == {}


def test_since_action_counts_only_the_watches_after_the_last_action():
    watch, act = {"kind": "watch", "at": 0}, {"kind": "act", "at": 0}
    assert activity.since_action([]) == 0
    assert activity.since_action([watch, watch]) == 2
    assert activity.since_action([watch, act, watch, act]) == 0
    assert activity.since_action([watch, act, watch, watch, act, watch]) == 1


def test_since_action_collapses_re_arms_as_tally_does():
    rearm = [{"kind": "watch", "rearm": True, "at": minute * 60_000} for minute in (0, 1, 31)]
    assert activity.since_action([{"kind": "act", "at": 0}, *rearm]) == 2


def test_rows_of_reads_one_agents_rows(tmp_path):
    activity.record("Monitor", {}, BOUND, tmp_path, now_ms=1)
    activity.record("Edit", {}, {**BOUND, "AGENTIHOOKS_AGENT_NAME": "sw-eng-2"}, tmp_path, now_ms=2)
    assert activity.rows_of("sw", "sw-eng-1", tmp_path) == [{"kind": "watch", "at": 1}]
    assert activity.rows_of("sw", "sw-eng-3", tmp_path) == []
    assert activity.rows_of("none", "sw-eng-1", tmp_path / "missing") == []


@pytest.mark.parametrize("slug", ["", ".", "..", "sw/..", "sw/eng", "absolute"])
def test_clear_refuses_a_name_that_is_not_one_folder_under_the_root(tmp_path, slug):
    root = tmp_path / "activity"
    activity.record("Monitor", {}, BOUND, root)
    with pytest.raises(ValueError, match="refusing to clear swarm activity for .*: not one folder under"):
        activity.clear(str(tmp_path) if slug == "absolute" else slug, root)
    assert activity.counts("sw", root) == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}


def test_clear_removes_only_the_named_swarm(tmp_path):
    root = tmp_path / "activity"
    activity.record("Monitor", {}, BOUND, root)
    activity.record("Monitor", {}, {**BOUND, "AGENTIHOOKS_SWARM": "other"}, root)
    activity.clear("sw", root)
    activity.clear("sw", root)
    assert activity.counts("sw", root) == {}
    assert activity.counts("other", root) == {"sw-eng-1": {"watch": 1, "act": 0, "since": 1}}
