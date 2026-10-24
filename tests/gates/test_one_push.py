import json
import subprocess
from pathlib import Path

import pytest

from hooks.targets.normalizer import normalize_payload
from scripts.gates import Call, Gate, Who, entry
from scripts.gates.one_push import OnePush, actions, destinations, open_refusal, push_refusal, queue_refusal
from scripts.gates.subagents import SubagentBudget
from scripts.gates.verdicts import Verdicts
from scripts.swarm.ledger_events import PullRequest, pull_request

ME = Who(name="ci@323133-0001", swarm="demo", lane="ci", task="t1")
URL = "https://github.com/o/r/pull/7"


def git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=a@b", "-c", "user.name=a", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def tree(tmp_path):
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "task", str(work))
    git(work, "remote", "add", "origin", "https://github.com/o/r.git")
    (work / "a.txt").write_text("a\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "a")
    git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
    return work


def state(tmp_path):
    return Verdicts("demo", "one-push", tmp_path)


def reviewed(tmp_path, *readers):
    budget = SubagentBudget()
    for reader in readers:
        budget.decide(Call("Agent", {"name": reader, "prompt": "review"}), ME, Verdicts("demo", "subagents", tmp_path))


class Ledger:
    def __init__(self, pr_url=URL, branch="task"):
        self.pr_url, self.branch = pr_url, branch

    def tasks(self, slug):
        return [{"id": "t1", "pr_url": self.pr_url, "branch": self.branch}] if slug == "demo" else []


def pull(state="OPEN", red=False, running=True, queued=False):
    return PullRequest(
        state=state,
        merged_at=None,
        pushed_at=None,
        red=red,
        resolved=not running,
        queued=queued,
        running=running,
        started=running,
    )


def gate(found=None, pr_url=URL, target="claude", branch="task"):
    return OnePush(
        ledger=lambda: Ledger(pr_url, branch), github=lambda url: found if url == pr_url else None, target=target
    )


def bash(command, cwd):
    return Call("Bash", {"command": command}, cwd=str(cwd))


def test_it_is_an_enforcing_gate_in_the_entry_list():
    found = OnePush()
    assert isinstance(found, Gate)
    assert (found.name, found.default_mode) == ("one-push", "enforce")
    assert isinstance(entry.GATES["one-push"], OnePush)


def test_it_matches_git_pushes_and_pull_request_opens_only():
    found = OnePush()
    assert found.matches(Call("Bash", {"command": "git push -u origin HEAD"}))
    assert found.matches(Call("Bash", {"command": "gh pr create --base dev"}))
    assert found.matches(Call("Bash", {"command": "gh pr ready 7"}))
    assert not found.matches(Call("Bash", {"command": "git status"}))
    assert not found.matches(Call("Bash", {"command": "gh pr view 7"}))
    assert not found.matches(Call("Read", {"command": "git push"}))


def test_actions_name_each_open_and_push_with_the_directory_it_runs_in():
    command = "cd /w && gh pr create --base dev; git -C sub push -u origin HEAD; gh pr ready 7"
    assert list(actions(command, "/home")) == [
        ("open", Path("/w"), ()),
        ("push", Path("/w/sub"), ["-u", "origin", "HEAD"]),
        ("open", Path("/w"), ()),
    ]
    assert list(actions("git -c a=b push", "/x")) == [("push", Path("/x"), [])]


def test_destinations_read_each_refspec_and_the_current_branch_for_head(tree):
    assert destinations(tree, []) == {"task"}
    assert destinations(tree, ["-u", "origin", "HEAD"]) == {"task"}
    assert destinations(tree, ["origin", "+HEAD:refs/heads/wip/x", "other"]) == {"wip/x", "other"}


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --draft --base dev",
        "gh pr create -d --base dev",
        "gh pr ready --undo 7",
        "git push --dry-run",
        "git push origin --delete old",
        "echo git push && echo gh pr create",
        "gh pr view 7",
    ],
)
def test_drafts_dry_runs_deletes_and_mentions_are_no_action(command):
    assert list(actions(command, "/x")) == []


def test_an_open_before_both_reviews_is_refused(tree, tmp_path):
    reviewed(tmp_path, "standards-reader")
    decision = gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path))
    assert not decision.allowed
    assert decision.reason == open_refusal("demo", ["spec-reader"], "") + f". Checked worktree: {tree}"


def test_an_open_after_both_reviews_with_the_work_on_origin_passes(tree, tmp_path):
    reviewed(tmp_path, "standards-reader", "spec-reader")
    assert gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed


@pytest.mark.parametrize("folder", ["cd", "workdir", "relative-workdir", "workdir-cd"])
@pytest.mark.parametrize("unpushed_command", [False, True])
def test_ready_counts_the_command_checkout_instead_of_the_session_checkout(tree, tmp_path, folder, unpushed_command):
    work = tmp_path / "command"
    git(tree, "worktree", "add", "--detach", str(work), "HEAD")
    changed = work if unpushed_command else tree
    (changed / "a.txt").write_text("unpublished\n")
    git(changed, "commit", "-q", "-am", "unpublished")
    tool_input = {"command": "gh pr ready 1"}
    if folder == "cd":
        tool_input["command"] = f"cd {work} && gh pr ready 1"
    elif folder == "workdir-cd":
        tool_input["workdir"] = str(tmp_path)
        tool_input["command"] = "cd command && gh pr ready 1"
    else:
        tool_input["workdir"] = "../command" if folder == "relative-workdir" else str(work)
    call = Call.from_payload({"tool_name": "Bash", "tool_input": tool_input, "cwd": str(tree)})
    decision = gate(target="codex").decide(call, ME, state(tmp_path))
    assert decision.allowed is not unpushed_command, decision.reason
    if unpushed_command:
        assert decision.reason == open_refusal("demo", [], "commits not on origin") + f". Checked worktree: {work}"


def test_ready_without_a_command_folder_counts_the_session_checkout(tree, tmp_path):
    (tree / "a.txt").write_text("unpublished\n")
    git(tree, "commit", "-q", "-am", "unpublished")
    decision = gate(target="codex").decide(bash("gh pr ready 1", tree), ME, state(tmp_path))
    assert decision.reason == open_refusal("demo", [], "commits not on origin") + f". Checked worktree: {tree}"


def codex_rollout(tmp_path, *programs, filler=0):
    rollout = tmp_path / "rollout.jsonl"
    lines = []
    for program in programs:
        item = {"type": "custom_tool_call", "status": "completed", "name": "exec", "input": program}
        lines.append(json.dumps({"type": "response_item", "payload": item}))
    if filler:
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "text": "x" * filler}}))
    lines += [
        "not json",
        "{}",
        "[]",
        '{"payload": "x"}',
        json.dumps({"type": "turn_context", "payload": {"cwd": "/elsewhere"}}),
    ]
    rollout.write_text("\n".join(lines) + "\n")
    return rollout


def exec_command(command, workdir=None):
    fields = f"cmd:{json.dumps(command)}"
    if workdir is not None:
        fields += f",workdir:{json.dumps(str(workdir))}"
    return f"text(await tools.exec_command({{{fields},max_output_tokens:2000}}));\n"


def codex_call(monkeypatch, rollout, command, cwd, **tool_input):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    payload = {
        "session_id": "01a15435-ba42-7c60-a0ac-3d9c038cf8c9",
        "turn_id": "01a15435-ba79-7630-9eff-1ef0d95d96a4",
        "transcript_path": str(rollout),
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "model": "gpt-6.1-sol",
        "permission_mode": "bypassPermissions",
        "tool_name": "Bash",
        "tool_input": {"command": command, **tool_input},
        "tool_use_id": "exec-b11bb50c-5652-4d33-85b1-d73603859b3c",
    }
    return Call.from_payload(normalize_payload(payload))


@pytest.mark.parametrize("unpushed_command", [False, True])
def test_ready_counts_the_codex_exec_workdir_from_the_rollout(monkeypatch, tree, tmp_path, unpushed_command):
    work = tmp_path / "command"
    git(tree, "worktree", "add", "--detach", str(work), "HEAD")
    changed = work if unpushed_command else tree
    (changed / "a.txt").write_text("unpublished\n")
    git(changed, "commit", "-q", "-am", "unpublished")
    command = "git status -sb\ngh pr ready 1"
    rollout = codex_rollout(tmp_path, exec_command("agentihooks msg read 1") + exec_command(command, work))
    decision = gate(target="codex").decide(codex_call(monkeypatch, rollout, command, tree), ME, state(tmp_path))
    assert decision.allowed is not unpushed_command, decision.reason
    if unpushed_command:
        assert decision.reason == open_refusal("demo", [], "commits not on origin") + f". Checked worktree: {work}"


def test_ready_reads_the_latest_codex_exec_program(monkeypatch, tree, tmp_path):
    work = tmp_path / "command"
    git(tree, "worktree", "add", "--detach", str(work), "HEAD")
    (tree / "a.txt").write_text("unpublished\n")
    git(tree, "commit", "-q", "-am", "unpublished")
    rollout = codex_rollout(tmp_path, exec_command("gh pr ready 1", tree), exec_command("gh pr ready 1", work))
    decision = gate(target="codex").decide(codex_call(monkeypatch, rollout, "gh pr ready 1", tree), ME, state(tmp_path))
    assert decision.allowed, decision.reason


@pytest.mark.parametrize(
    "program",
    ["no-workdir", "other-command", "two-workdirs", "malformed", "undecodable", "beyond-tail", "no-rollout"],
)
def test_ready_without_a_codex_exec_workdir_counts_the_session_checkout(monkeypatch, tree, tmp_path, program):
    work = tmp_path / "command"
    git(tree, "worktree", "add", "--detach", str(work), "HEAD")
    (tree / "a.txt").write_text("unpublished\n")
    git(tree, "commit", "-q", "-am", "unpublished")
    earlier = exec_command("gh pr ready 1", work)
    rollouts = {
        "no-workdir": lambda: codex_rollout(tmp_path, earlier, exec_command("gh pr ready 1")),
        "other-command": lambda: codex_rollout(tmp_path, earlier, exec_command("gh pr ready 2", work)),
        "two-workdirs": lambda: codex_rollout(
            tmp_path, earlier, exec_command("gh pr ready 1", work) + exec_command("gh pr ready 1", tree)
        ),
        "malformed": lambda: codex_rollout(tmp_path, earlier + 'exec_command({cmd:"gh pr ready 2",workdir:"open'),
        "undecodable": lambda: codex_rollout(tmp_path, 'exec_command({cmd:"gh pr ready 1",workdir:"/x\\x2"});'),
        "beyond-tail": lambda: codex_rollout(tmp_path, earlier, filler=1 << 20),
        "no-rollout": lambda: tmp_path / "missing.jsonl",
    }
    call = codex_call(monkeypatch, rollouts[program](), "gh pr ready 1", tree)
    assert "workdir" not in call.tool_input
    decision = gate(target="codex").decide(call, ME, state(tmp_path))
    assert decision.reason == open_refusal("demo", [], "commits not on origin") + f". Checked worktree: {tree}"


def test_a_codex_payload_workdir_wins_over_the_rollout(monkeypatch, tree, tmp_path):
    work = tmp_path / "command"
    rollout = codex_rollout(tmp_path, exec_command("gh pr ready 1", tree))
    call = codex_call(monkeypatch, rollout, "gh pr ready 1", tree, workdir=str(work))
    assert call.tool_input["workdir"] == str(work)


def test_a_claude_payload_never_reads_a_codex_rollout(monkeypatch, tree, tmp_path):
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    rollout = codex_rollout(tmp_path, exec_command("gh pr ready 1", tmp_path))
    payload = {"transcript_path": str(rollout), "tool_name": "Bash", "tool_input": {"command": "gh pr ready 1"}}
    assert normalize_payload(payload)["tool_input"] == {"command": "gh pr ready 1"}


def test_a_reader_named_by_its_sub_agent_type_counts(tree, tmp_path):
    budget = SubagentBudget()
    for reader in ("standards-reader", "spec-reader"):
        budget.decide(Call("Task", {"subagent_type": reader}), ME, Verdicts("demo", "subagents", tmp_path))
    assert gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed


def test_an_open_over_uncommitted_or_unpushed_work_is_refused(tree, tmp_path):
    reviewed(tmp_path, "standards-reader", "spec-reader")
    (tree / "a.txt").write_text("b\n")
    decision = gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path))
    assert decision.reason == open_refusal("demo", [], "uncommitted changes") + f". Checked worktree: {tree}"
    git(tree, "commit", "-q", "-am", "b")
    decision = gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path))
    assert decision.reason == open_refusal("demo", [], "commits not on origin") + f". Checked worktree: {tree}"


def test_a_draft_open_passes_before_any_review(tree, tmp_path):
    (tree / "a.txt").write_text("b\n")
    assert gate().decide(bash("gh pr create --draft --base dev", tree), ME, state(tmp_path)).allowed


def test_a_harness_without_sub_agents_is_not_asked_for_readers(tree, tmp_path):
    assert gate(target="codex").decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed


def test_a_push_while_checks_run_and_none_is_red_is_refused(tree, tmp_path):
    decision = gate(pull()).decide(bash("git push", tree), ME, state(tmp_path))
    assert not decision.allowed
    assert decision.reason == push_refusal("demo", URL)


@pytest.mark.parametrize(
    "found",
    [pull(red=True), pull(running=False), pull(state="MERGED"), pull(state="CLOSED"), None],
)
def test_a_push_passes_once_a_check_is_red_or_checks_resolved_or_the_pull_request_is_gone(tree, tmp_path, found):
    assert gate(found).decide(bash("git push", tree), ME, state(tmp_path)).allowed


def test_a_push_passes_with_no_pull_request_or_into_another_repo(tree, tmp_path):
    assert gate(pull(), pr_url=None).decide(bash("git push -u origin HEAD", tree), ME, state(tmp_path)).allowed
    other = "https://github.com/o/elsewhere/pull/3"
    assert gate(pull(), pr_url=other).decide(bash("git push", tree), ME, state(tmp_path)).allowed


def test_a_push_to_another_branch_passes_and_one_to_the_task_branch_is_held(tree, tmp_path):
    assert gate(pull()).decide(bash("git push origin HEAD:wip/x", tree), ME, state(tmp_path)).allowed
    assert not gate(pull()).decide(bash("git push origin task", tree), ME, state(tmp_path)).allowed
    assert not gate(pull(), branch=None).decide(bash("git push origin HEAD:wip/x", tree), ME, state(tmp_path)).allowed


def test_sessions_outside_a_worker_task_are_never_held(tree, tmp_path):
    for who in (
        Who(),
        Who(name="master@1-1", swarm="demo", lane="master", task="t1"),
        Who(name="ci@1-1", swarm="demo"),
    ):
        assert gate(pull()).decide(bash("git push; gh pr create --base dev", tree), who, state(tmp_path)).allowed


def test_the_refusals_name_what_is_owed_and_the_way_out():
    assert open_refusal("demo", ["spec-reader"], "commits not on origin") == (
        "open the pull request once review closes, so one push carries it: launch the spec-reader sub-agent on the "
        "committed diff and close its findings; commit and push the commits not on origin. A draft pull request for "
        'a block stays allowed: gh pr create --draft, then agentihooks swarm demo block "<why>"'
    )
    assert push_refusal("demo", URL) == (
        f"checks still run on {URL} and none is red: a push now cancels them. Wait with agentihooks swarm demo wait "
        f"--on checks {URL}, then push once they resolve, or as soon as a check goes red"
    )


def test_a_push_while_the_pull_request_waits_in_the_merge_queue_is_refused(tree, tmp_path):
    decision = gate(pull(running=False, queued=True)).decide(bash("git push", tree), ME, state(tmp_path))
    assert decision.reason == queue_refusal("demo", URL)
    assert gate(pull(running=False, queued=True, red=True)).decide(bash("git push", tree), ME, state(tmp_path)).allowed


NEVER_STARTED = {"state": "OPEN", "gated": True, "gate_workflows": ["tests.yml"]}
LINT_EXPECTED = [{"context": "lint", "state": "EXPECTED"}]
TESTS_RUNNING = [{"status": "IN_PROGRESS", "workflowRun": {"databaseId": 1}}]


@pytest.mark.parametrize(
    "raw",
    [
        {**NEVER_STARTED, "mergeable": "CONFLICTING"},
        {**NEVER_STARTED, "mergeable": "CONFLICTING", "statusCheckRollup": LINT_EXPECTED},
        {**NEVER_STARTED, "mergeable": "MERGEABLE"},
    ],
)
def test_a_required_workflow_that_never_started_lets_the_push_pass(tree, tmp_path, raw):
    found = pull_request(raw)
    assert found.running
    assert gate(found).decide(bash("git push", tree), ME, state(tmp_path)).allowed


@pytest.mark.parametrize(
    "raw",
    [
        {**NEVER_STARTED, "mergeable": "CONFLICTING", "checkSuites": TESTS_RUNNING},
        {**NEVER_STARTED, "mergeable": "CONFLICTING", "statusCheckRollup": [{"context": "lint", "state": "PENDING"}]},
        {**NEVER_STARTED, "mergeable": "MERGEABLE", "statusCheckRollup": LINT_EXPECTED},
    ],
)
def test_a_real_running_check_or_a_pending_placeholder_on_a_mergeable_head_still_holds_the_push(tree, tmp_path, raw):
    decision = gate(pull_request(raw)).decide(bash("git push", tree), ME, state(tmp_path))
    assert not decision.allowed
    assert decision.reason == push_refusal("demo", URL)


def test_a_reader_counts_when_either_its_name_or_its_type_names_it(tree, tmp_path):
    budget = SubagentBudget()
    for reader in ("standards-reader", "spec-reader"):
        call = Call("Agent", {"name": f"{reader}-2", "subagent_type": reader})
        budget.decide(call, ME, Verdicts("demo", "subagents", tmp_path))
    assert gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed


def test_gh_repo_flags_before_the_command_still_name_an_open():
    assert [kind for kind, _, _ in actions("gh -R o/r pr create --base dev; gh --repo=o/r pr ready 7", "/x")] == [
        "open",
        "open",
    ]


def test_a_push_names_its_own_remote_for_the_repo_check(tree, tmp_path):
    git(tree, "remote", "add", "fork", "https://github.com/o/elsewhere.git")
    assert gate(pull()).decide(bash("git push fork task", tree), ME, state(tmp_path)).allowed
    held = "git push https://github.com/o/r.git task"
    assert not gate(pull()).decide(bash(held, tree), ME, state(tmp_path)).allowed
    assert gate(pull()).decide(bash("git push https://github.com/o/other.git task", tree), ME, state(tmp_path)).allowed


def test_the_queue_refusal_names_the_dequeue():
    assert queue_refusal("demo", URL) == (
        f"{URL} waits in the merge queue: a push now drops it. Dequeue first with agentihooks swarm demo merge "
        f"dequeue {URL}, then push"
    )


def test_push_options_taking_a_value_are_not_read_as_the_remote(tree, tmp_path):
    git(tree, "remote", "add", "fork", "https://github.com/o/elsewhere.git")
    assert gate(pull()).decide(bash("git push -o ci.skip fork task", tree), ME, state(tmp_path)).allowed
    assert destinations(tree, ["--push-option", "x", "origin", "HEAD:wip/y"]) == {"wip/y"}


def test_git_options_before_the_command_are_skipped_and_a_bare_dash_c_names_nothing():
    assert list(actions("git --no-pager -C sub push origin", "/x")) == [("push", Path("/x/sub"), ["origin"])]
    assert list(actions("git -C", "/x")) == []
    assert list(actions("git push", None)) == [("push", Path("."), [])]
    assert list(actions("FOO=1; git push", "/x")) == [("push", Path("/x"), [])]


def test_untracked_files_do_not_hold_an_open_and_a_folder_outside_git_holds_nothing(tree, tmp_path):
    reviewed(tmp_path, "standards-reader", "spec-reader")
    (tree / "notes.txt").write_text("scratch\n")
    assert gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gate().decide(bash("gh pr create --base dev", plain), ME, state(tmp_path)).allowed


def test_a_trailing_valued_option_and_a_forced_refspec_still_name_the_branch(tree):
    assert destinations(tree, ["origin", "+task", "-o"]) == {"task"}
    assert destinations(tree, ["origin", "+Xtask"]) == {"Xtask"}


def test_a_remote_url_in_another_case_still_names_the_pull_request_repo(tree, tmp_path):
    assert not gate(pull()).decide(bash("git push https://github.com/O/R.git task", tree), ME, state(tmp_path)).allowed


def test_a_push_to_a_remote_outside_github_is_held_as_the_task_repo(tree, tmp_path):
    assert not gate(pull()).decide(bash("git push /srv/mirror.git task", tree), ME, state(tmp_path)).allowed


def test_the_open_refusal_names_both_readers_when_neither_ran():
    assert open_refusal("demo", ["standards-reader", "spec-reader"], "") == (
        "open the pull request once review closes, so one push carries it: launch the standards-reader and "
        "spec-reader sub-agents on the committed diff and close its findings. A draft pull request for a block stays "
        'allowed: gh pr create --draft, then agentihooks swarm demo block "<why>"'
    )


def test_a_session_with_a_task_but_no_swarm_is_never_held(tree, tmp_path):
    who = Who(name=ME.name, lane="ci", task="t1")
    assert gate(pull()).decide(bash("git push; gh pr create --base dev", tree), who, state(tmp_path)).allowed


def test_a_push_passes_when_the_ledger_has_no_such_task(tree, tmp_path):
    other = Who(name="ci@323133-0001", swarm="demo", lane="ci", task="t9")
    assert gate(pull()).decide(bash("git push", tree), other, state(tmp_path)).allowed


@pytest.mark.parametrize(("environ", "allowed"), [({"AGENTIHOOKS_TARGET": "codex"}, True), ({}, False)])
def test_the_harness_comes_from_the_environment_and_defaults_to_claude(tree, tmp_path, monkeypatch, environ, allowed):
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    found = OnePush(ledger=lambda: Ledger(), github=lambda url: None)
    assert found.target() == environ.get("AGENTIHOOKS_TARGET", "claude")
    assert found.decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed is allowed


def test_a_codex_payload_without_a_transcript_path_finds_its_rollout(monkeypatch, tree, tmp_path):
    work = tmp_path / "command"
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "19"
    day.mkdir(parents=True)
    rollout = codex_rollout(tmp_path, exec_command("gh pr ready 1", work))
    rollout.rename(day / "rollout-2026-10-19T14-49-29-01a15435-ba42-7c60-a0ac-3d9c038cf8c9.jsonl")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    payload = {
        "session_id": "01a15435-ba42-7c60-a0ac-3d9c038cf8c9",
        "cwd": str(tree),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr ready 1"},
    }
    assert normalize_payload(payload)["tool_input"]["workdir"] == str(work)


@pytest.mark.parametrize("shift", [0, 4])
def test_the_rollout_tail_holds_a_whole_exec_program(monkeypatch, tree, tmp_path, shift):
    work = tmp_path / "command"
    item = {"type": "custom_tool_call", "name": "exec", "input": exec_command("gh pr ready 1", work)}
    program = (json.dumps({"type": "response_item", "payload": item}) + "\n").encode()
    head = ('{"text": "' + "\u00e9" * (1 << 20) + '"}\n').encode()
    rest = (1 << 20) - shift - len(program)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(head + program + b" " * (rest - 1) + b"\n")
    call = codex_call(monkeypatch, rollout, "gh pr ready 1", tree)
    assert call.tool_input["workdir"] == str(work)


@pytest.mark.parametrize("unpushed_command", [False, True])
def test_ready_honors_json_quoted_exec_fields(monkeypatch, tree, tmp_path, unpushed_command):
    work = tmp_path / "command"
    git(tree, "worktree", "add", "--detach", str(work), "HEAD")
    changed = work if unpushed_command else tree
    (changed / "a.txt").write_text("unpublished\n")
    git(changed, "commit", "-q", "-am", "unpublished")
    command = "gh pr ready 1"
    program = "await tools.exec_command(" + json.dumps({"cmd": command, "workdir": str(work)}) + ");"
    rollout = codex_rollout(tmp_path, program)
    decision = gate(target="codex").decide(codex_call(monkeypatch, rollout, command, tree), ME, state(tmp_path))
    assert decision.allowed is not unpushed_command, decision.reason
    if unpushed_command:
        assert f"Checked worktree: {work}" in decision.reason


def test_ready_refusal_names_the_shell_checkout(tree, tmp_path):
    (tree / "a.txt").write_text("unpublished\n")
    decision = gate(target="codex").decide(bash(f"cd {tree} && gh pr ready 1", tmp_path), ME, state(tmp_path))
    assert not decision.allowed
    assert f"Checked worktree: {tree}" in decision.reason


@pytest.fixture
def other_repo(tmp_path):
    work = tmp_path / "other"
    git(tmp_path, "init", "-q", "-b", "task", str(work))
    git(work, "remote", "add", "origin", "https://github.com/o/b.git")
    return work


def test_a_cross_repo_push_through_a_shell_variable_passes_while_the_other_repos_checks_run(
    tree, other_repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("OTHER_REPO", str(other_repo))
    for command in (
        f'B={other_repo}; cd "$B" && git push origin task',
        'cd "$OTHER_REPO" && git push origin task',
        "git -C ${OTHER_REPO} push origin HEAD:task",
    ):
        assert gate(pull()).decide(bash(command, tree), ME, state(tmp_path)).allowed, command


def test_a_push_through_a_shell_variable_into_the_pull_request_repo_is_still_refused(tree, other_repo, tmp_path):
    decision = gate(pull()).decide(bash(f'A={tree}; cd "$A" && git push origin task', other_repo), ME, state(tmp_path))
    assert (decision.allowed, decision.reason) == (False, push_refusal("demo", URL))
    assert not gate(pull()).decide(bash("git push origin task", tree), ME, state(tmp_path)).allowed


def test_a_push_from_a_folder_that_is_no_checkout_holds_nothing(tree, tmp_path):
    command = f"cd {tmp_path / 'missing'} && git push origin task"
    assert gate(pull()).decide(bash(command, tree), ME, state(tmp_path)).allowed


def test_an_unset_variable_or_an_exported_one_still_finds_the_pull_request_checkout(tree, other_repo, tmp_path):
    for command, cwd in (
        ('cd "$NO_SUCH_REPO_FOLDER" && git push origin task', tree),
        (f'export A={tree}; cd "$A" && git push origin task', other_repo),
        (f'export ROOT={tree.parent} X=1; cd "${{ROOT}}/work" && git push origin task', other_repo),
        (f'declare A={tree}; cd "$A" && git push origin task', other_repo),
        (f"pushd {tree} && git push origin task", other_repo),
        (f"cd -P -- {tree} && git push origin task", other_repo),
        (f"cd {tree}; cd {other_repo}; cd - && git push origin task", other_repo),
    ):
        decision = gate(pull()).decide(bash(command, cwd), ME, state(tmp_path))
        assert (decision.allowed, decision.reason) == (False, push_refusal("demo", URL)), command


def test_a_folder_named_by_a_command_substitution_is_held_as_the_task_repo(tree, other_repo, tmp_path):
    command = 'B=$(wt.sh path); cd "$B" && git push origin task'
    assert not gate(pull()).decide(bash(command, other_repo), ME, state(tmp_path)).allowed
