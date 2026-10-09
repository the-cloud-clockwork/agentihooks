import subprocess
from pathlib import Path

import pytest

from scripts.gates import Call, Gate, Who, entry
from scripts.gates.one_push import OnePush, actions, destinations, open_refusal, push_refusal, queue_refusal
from scripts.gates.subagents import SubagentBudget
from scripts.gates.verdicts import Verdicts
from scripts.swarm.ledger_events import PullRequest

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
        state=state, merged_at=None, pushed_at=None, red=red, resolved=not running, queued=queued, running=running
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
    assert decision.reason == open_refusal("demo", ["spec-reader"], "")


def test_an_open_after_both_reviews_with_the_work_on_origin_passes(tree, tmp_path):
    reviewed(tmp_path, "standards-reader", "spec-reader")
    assert gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed


def test_a_reader_named_by_its_sub_agent_type_counts(tree, tmp_path):
    budget = SubagentBudget()
    for reader in ("standards-reader", "spec-reader"):
        budget.decide(Call("Task", {"subagent_type": reader}), ME, Verdicts("demo", "subagents", tmp_path))
    assert gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path)).allowed


def test_an_open_over_uncommitted_or_unpushed_work_is_refused(tree, tmp_path):
    reviewed(tmp_path, "standards-reader", "spec-reader")
    (tree / "a.txt").write_text("b\n")
    decision = gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path))
    assert decision.reason == open_refusal("demo", [], "uncommitted changes")
    git(tree, "commit", "-q", "-am", "b")
    decision = gate().decide(bash("gh pr create --base dev", tree), ME, state(tmp_path))
    assert decision.reason == open_refusal("demo", [], "commits not on origin")


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
    who = Who(name="ci@1-1", lane="ci", task="t1")
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
