import subprocess
import sys

TARGET = 'tests/gates/test_push_stop.py::test_a_retained_branch_deleted_from_origin_is_not_recreated'
BEFORE = ['tests/gates/test_push_stop.py::test_a_bare_wait_another_kind_or_a_missing_or_own_task_still_refuses_the_stop[on2]', 'tests/gates/test_push_stop.py::test_a_branch_with_no_work_of_its_own_stops_freely_after_a_pushed_outcome', 'tests/gates/test_push_stop.py::test_a_checked_wait_on_an_open_task_lets_a_refused_push_stop[open]', 'tests/gates/test_push_stop.py::test_a_checked_wait_on_an_open_task_lets_a_refused_push_stop[pr]', 'tests/gates/test_push_stop.py::test_a_closed_template_item_is_sent_again', 'tests/gates/test_push_stop.py::test_a_count_git_cannot_answer_is_zero', 'tests/gates/test_push_stop.py::test_a_fresh_worktree_with_no_work_stops_freely', 'tests/gates/test_push_stop.py::test_a_gate_left_just_under_the_floor_never_starts', 'tests/gates/test_push_stop.py::test_a_later_stop_that_passes_closes_the_open_template_item_with_its_outcome', 'tests/gates/test_push_stop.py::test_a_live_merge_wait_on_an_open_pull_request_lets_a_refused_push_stop[https://github.com/o/r/pull/7]', 'tests/gates/test_push_stop.py::test_a_live_merge_wait_on_an_open_pull_request_lets_a_refused_push_stop[https://github.com/o/r/pull/8]', 'tests/gates/test_push_stop.py::test_a_master_a_planner_and_the_operators_sessions_are_never_gated[who1]', 'tests/gates/test_push_stop.py::test_a_master_a_planner_and_the_operators_sessions_are_never_gated[who2]', 'tests/gates/test_push_stop.py::test_a_merge_wait_on_a_pull_request_no_longer_open_still_refuses_the_stop[MERGED]', 'tests/gates/test_push_stop.py::test_a_merge_wait_on_a_pull_request_no_longer_open_still_refuses_the_stop[None]', 'tests/gates/test_push_stop.py::test_a_passing_pre_push_gate_runs_in_the_worktree_with_the_local_test_setting_then_pushes', 'tests/gates/test_push_stop.py::test_a_plan_task_is_never_gated', 'tests/gates/test_push_stop.py::test_a_pre_push_gate_killed_by_a_signal_keeps_the_branch_off_origin', 'tests/gates/test_push_stop.py::test_a_pre_push_gate_past_its_timeout_is_killed_with_its_children_and_keeps_the_branch_off_origin', 'tests/gates/test_push_stop.py::test_a_protected_or_fresh_continuation_pushes_its_own_branch[]', 'tests/gates/test_push_stop.py::test_a_protected_or_fresh_continuation_pushes_its_own_branch[fresh]', 'tests/gates/test_push_stop.py::test_a_pull_request_in_another_repo_does_not_answer_for_pushed_work_in_this_one', 'tests/gates/test_push_stop.py::test_a_push_the_agent_made_itself_counts_from_its_recorded_outcome', 'tests/gates/test_push_stop.py::test_a_pushed_head_with_dirty_files_never_runs_the_gate', 'tests/gates/test_push_stop.py::test_a_pushed_outcome_at_zero_is_answered_only_by_a_later_line', 'tests/gates/test_push_stop.py::test_a_redis_error_reading_fleet_panic_reads_as_no_panic_so_the_hook_pushes']


def run(ids):
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-o", "addopts=", "-p", "no:xdist", "-p", "no:randomly", "-p", "no:cacheprovider", *ids],
        capture_output=True,
        text=True,
    )
    return done.returncode, done.stdout[-3000:]


def test_order_probe():
    results = []
    code, out = run([TARGET])
    results.append(("alone", code))
    code, out = run([*BEFORE, TARGET])
    results.append(("sequence", code))
    print(out)
    for name in BEFORE:
        code, _ = run([name, TARGET])
        results.append((name, code))
    for row in results:
        print("PROBE", row)
    assert False, results
