import subprocess
from pathlib import Path

import pytest

from scripts.gates import Call, Gate, Who, entry
from scripts.gates.one_push import OnePush, actions, open_refusal, push_refusal
from scripts.gates.subagents import SubagentBudget
from scripts.gates.verdicts import Verdicts
from scripts.swarm.ledger_events import PullRequest

ME = Who(name="ci@1-1", swarm="demo", lane="ci", task="t1")
URL = "https://github.com/o/r/pull/7"


def git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=a@b", "-c", "user.name=a", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def tree(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-q", str(origin))
    git(tmp_path, "init", "-q", str(work))
    git(work, "remote", "add", "origin", "https://github.com/o/r.git")
    git(work, "config", "remote.origin.pushurl", str(origin))
    (work / "a.txt").write_text("a\n")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "a")
    git(work, "push", "-q", str(origin), "HEAD:refs/heads/main")
    git(work, "fetch", "-q", "origin")
    return work


def state(tmp_path):
    return Verdicts("demo", "one-push", tmp_path)


def reviewed(tmp_path, *readers):
    budget = SubagentBudget()
    for reader in readers:
        budget.decide(Call("Agent", {"name": reader, "prompt": "review"}), ME, Verdicts("demo", "subagents", tmp_path))


class Ledger:
    def __init__(self, pr_url=URL):
        self.pr_url = pr_url

    def tasks(self, slug):
        return [{"id": "t1", "pr_url": self.pr_url}]


def pull(state="OPEN", red=False, resolved=False):
    return PullRequest(state=state, merged_at=None, pushed_at=None, red=red, resolved=resolved)


def gate(found=None, pr_url=URL, target="claude"):
    return OnePush(ledger=lambda: Ledger(pr_url), github=lambda url: found, target=target)


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
        ("open", Path("/w")),
        ("push", Path("/w/sub")),
        ("open", Path("/w")),
    ]
    assert list(actions("git -c a=b push", "/x")) == [("push", Path("/x"))]


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
    [pull(red=True), pull(resolved=True), pull(state="MERGED"), pull(state="CLOSED"), None],
)
def test_a_push_passes_once_a_check_is_red_or_checks_resolved_or_the_pull_request_is_gone(tree, tmp_path, found):
    assert gate(found).decide(bash("git push", tree), ME, state(tmp_path)).allowed


def test_a_push_passes_with_no_pull_request_or_into_another_repo(tree, tmp_path):
    assert gate(pull(), pr_url=None).decide(bash("git push -u origin HEAD", tree), ME, state(tmp_path)).allowed
    other = "https://github.com/o/elsewhere/pull/3"
    assert gate(pull(), pr_url=other).decide(bash("git push", tree), ME, state(tmp_path)).allowed


def test_sessions_outside_a_worker_task_are_never_held(tree, tmp_path):
    for who in (Who(), Who(name="master@1-1", swarm="demo", lane="master", task="t1"), Who(name="ci@1-1", swarm="demo")):
        assert gate(pull()).decide(bash("git push; gh pr create --base dev", tree), who, state(tmp_path)).allowed


def test_the_refusals_name_what_is_owed_and_the_way_out():
    assert open_refusal("demo", ["spec-reader"], "commits not on origin") == (
        "open the pull request once review closes, so one push carries it: launch the spec-reader sub agent on the "
        "committed diff and close its findings; commit and push the commits not on origin. A draft pull request for "
        'a block stays allowed: gh pr create --draft, then agentihooks swarm demo block "<why>"'
    )
    assert push_refusal("demo", URL) == (
        f"checks still run on {URL} and none is red: a push now cancels them. Wait with agentihooks swarm demo wait "
        f"--on checks {URL}, then push once they resolve, or as soon as a check goes red"
    )
