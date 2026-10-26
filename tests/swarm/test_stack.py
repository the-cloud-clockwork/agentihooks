import dataclasses
import json
import subprocess

import pytest

from scripts.gates import Call, Who
from scripts.gates.base import Decision
from scripts.gates.claim_stop import ClaimStop
from scripts.gates.verdicts import Verdicts
from scripts.handoff import check as handoff_check
from scripts.handoff import transfers
from scripts.inbox.seats import SeatRegistry
from scripts.swarm import claim_hold, cli, dispatcher, stack, tick
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_comments, task_dependencies
from tests.swarm.test_cli import HANDOFF, env

_fixture = env
AGENT = "engineer@a1b2c3-0001"
HEAD = "a" * 40
BASE = "b" * 40
ISSUE = "https://github.com/o/r/issues/7"
NOW = 5_000
NOTE = "The next engineer restacks it onto dev and finishes it."
READ_FIRST = "- ledger:sw/tasks/t1 the task and its contract\n"
ROOT = "/home/me/dev/worktrees"
TOP = f"{ROOT}/repo/engineer-a1b2c3-0001"
LOCATE = [
    ["git", "rev-parse", "--show-toplevel"],
    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
    ["bash", str(stack.WT_SCRIPT), "root"],
]
REMOVE = ["bash", str(stack.WT_SCRIPT), "done", "engineer-a1b2c3-0001", "--repo", TOP, "--pushed"]


class Shell:
    def __init__(self):
        self.calls, self.remote, self.issues, self.fail = [], HEAD, json.dumps({"hasIssuesEnabled": True}), ()
        self.status, self.top, self.origin, self.probe = "", TOP, "git@github.com:O/r.git", ""

    def __call__(self, argv):
        self.calls.append(argv)
        code = 1 if tuple(argv[:3]) in self.fail else 0
        out = {
            ("git", "remote", "get-url"): self.origin,
            ("git", "rev-parse", "--show-toplevel"): self.top,
            ("git", "rev-parse", "--path-format=absolute"): "/home/me/dev/repo/.git",
            ("bash", str(stack.WT_SCRIPT), "root"): ROOT,
            ("git", "status", "--porcelain"): self.status,
        }.get(tuple(argv[:3])) or {
            ("git", "rev-parse"): HEAD,
            ("git", "ls-remote"): self.probe + (f"{self.remote}\trefs/heads/{argv[-1]}" if self.remote else ""),
            ("git", "merge-base"): BASE,
            ("git", "rev-list"): "3",
            ("gh", "repo"): self.issues,
        }.get(tuple(argv[:2]), "")
        return subprocess.CompletedProcess(argv, code, stdout=out + "\n", stderr=" boom\n" if code else "")

    def comments(self):
        return [c for c in self.calls if c[:3] == ["gh", "issue", "comment"] and tuple(c[:3]) not in self.fail]


def _slug_checked(ledger, name):
    method = getattr(ledger, name)

    def checked(slug, *args, **kwargs):
        assert slug == "sw"
        return method(slug, *args, **kwargs)

    setattr(ledger, name, checked)


@pytest.fixture
def parked(env, monkeypatch, tmp_path):
    store, ledger, rt = env
    monkeypatch.setenv("HOME", str(tmp_path))
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    ledger.rows["a"] = {"id": "a", "title": "Build the parser", "state": "pr", "branch": "eng-a", "lane": "eng"}
    ledger.rows["b"] = {"id": "b", "title": "Write the schema", "state": "done", "branch": "eng-b", "lane": "eng"}
    ledger.rows["t1"].update({"depends_on": ["a", "b"], "branch": "eng-t1", "issue_url": ISSUE})
    ledger.updates = []
    update = ledger.update_task
    ledger.update_task = lambda slug, task, fields, by="swarm", if_state=(): (
        ledger.updates.append((task, fields, by)) or update(slug, task, fields, by, if_state)
    )
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((slug, task, text, by))
    for name in ("state", "tasks", "comment", "update_task"):
        _slug_checked(ledger, name)
    shell = Shell()
    monkeypatch.setattr(stack, "shell", shell)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    store.memory.add_recap("eng-1@sw", "engineer@a1b2c3-0000", "t1", "an earlier recap", 1)
    (tmp_path / ".agentihooks" / "swarm" / "sw" / "tasks" / "t1").mkdir(parents=True)
    (tmp_path / ".agentihooks" / "swarm" / "sw" / "tasks" / "t1" / "progress.md").write_text("- started\n")
    read_first = READ_FIRST + "- recap:eng-1@sw the earlier recap\n- workspace:t1/progress the step log\n"
    doc = tmp_path / "handoff.md"
    doc.write_text(HANDOFF.format(intent="Finish the parser task for the swarm.").replace(READ_FIRST, read_first))
    return store, ledger, rt, shell, doc


def park(doc):
    return cli.main(["sw", "--as", AGENT, "park", str(doc)])


def _refused(parked, capsys, says):
    store, ledger, _, shell, doc = parked
    assert park(doc) == 1
    assert capsys.readouterr().err == f"swarm: {says}\n"
    assert "parked_on" not in ledger.rows["t1"] and "stacked_base" not in ledger.rows["t1"]
    assert [row for row in ledger.comments if row[-1] != dispatcher.AUTHOR] == [] and shell.comments() == []
    assert REMOVE not in shell.calls
    assert store.handoff("sw", "t1") == ""
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["working"]


def test_park_refuses_a_task_without_a_recorded_branch(parked, capsys):
    del parked[1].rows["t1"]["branch"]
    _refused(parked, capsys, "the task records no branch; push it and run swarm branch before parking")


def test_park_refuses_a_branch_missing_from_origin(parked, capsys):
    parked[3].remote = ""
    _refused(parked, capsys, "branch eng-t1 is not on origin; push it first with git push -u origin eng-t1")
    assert parked[3].calls == [["git", "ls-remote", "--heads", "origin", "eng-t1"]]


def test_park_refuses_a_branch_with_unpushed_commits(parked, capsys):
    parked[3].remote = "c" * 40
    _refused(parked, capsys, "the worktree holds commits origin lacks; push eng-t1 first")
    assert parked[3].calls[-1] == ["git", "rev-parse", "HEAD"]


def test_park_accepts_a_synced_branch_beside_a_same_named_diffcheck_branch(parked, capsys):
    parked[3].probe = f"{'c' * 40}\trefs/heads/diffcheck/eng-t1\n"
    assert park(parked[4]) == 0
    assert json.loads(capsys.readouterr().out)["parked_on"] == ["a"]


def test_park_refuses_a_branch_only_a_same_named_diffcheck_branch_carries(parked, capsys):
    parked[3].probe, parked[3].remote = f"{HEAD}\trefs/heads/diffcheck/eng-t1\n", ""
    _refused(parked, capsys, "branch eng-t1 is not on origin; push it first with git push -u origin eng-t1")


def test_park_refuses_a_worktree_with_uncommitted_changes(parked, capsys):
    parked[3].status = " M scripts/x.py"
    _refused(parked, capsys, "the worktree holds uncommitted changes; commit them and push eng-t1 first")
    assert parked[3].calls[-1] == ["git", "status", "--porcelain"]


@pytest.mark.parametrize(
    "top", ["/elsewhere/engineer-a1b2c3-0001", f"{ROOT}/other/engineer-a1b2c3-0001", "/home/me/dev/repo"]
)
def test_park_refuses_a_worktree_outside_the_worktree_script_root(parked, capsys, top):
    parked[3].top = top
    _refused(parked, capsys, f"park and ready remove only a worktree wt.sh new made under {ROOT}; {top} is not one")
    assert parked[3].calls[-3:] == LOCATE


def test_park_refuses_a_task_without_an_issue_where_the_repo_has_issues(parked, capsys):
    parked[1].rows["t1"]["issue_url"] = ""
    _refused(parked, capsys, "the repo has issues; open one and run swarm issue before parking")
    assert parked[3].calls[-1] == ["gh", "repo", "view", "--json", "hasIssuesEnabled"]


def test_park_refuses_an_unreadable_answer_on_issues(parked, capsys):
    parked[1].rows["t1"]["issue_url"] = ""
    parked[3].issues = "{}"
    _refused(parked, capsys, "cannot tell whether the repo has issues: 'hasIssuesEnabled'")


@pytest.mark.parametrize(
    ("failing", "says"),
    [
        (("git", "rev-parse", "HEAD"), "cannot read the worktree head"),
        (("git", "status", "--porcelain"), "cannot inspect the worktree"),
        (("git", "rev-parse", "--show-toplevel"), "cannot locate the worktree"),
        (("git", "rev-parse", "--path-format=absolute"), "cannot locate the repo"),
        (("bash", str(stack.WT_SCRIPT), "root"), "cannot locate the worktree root"),
        (("gh", "repo", "view"), "cannot tell whether the repo has issues"),
        (("git", "fetch", "origin"), "cannot fetch eng-a"),
        (("git", "merge-base", "HEAD"), "eng-a shares no history"),
        (("git", "rev-list", "--count"), f"cannot count {BASE}"),
    ],
)
def test_park_refuses_when_a_command_fails(parked, capsys, failing, says):
    if failing[1] == "repo":
        parked[1].rows["t1"]["issue_url"] = ""
    parked[3].fail = (failing,)
    _refused(parked, capsys, f"{says}: boom")


def test_park_refuses_a_task_with_no_open_dependency(parked, capsys):
    parked[1].rows["a"]["state"] = "done"
    _refused(parked, capsys, "the task has no open dependency to park on; finish it instead")


def test_park_refuses_when_no_open_dependency_records_a_branch(parked, capsys):
    parked[1].rows["a"]["branch"] = ""
    _refused(parked, capsys, "no open dependency records a branch, so there is nothing to stack on")


def _branchless_ops(ledger):
    ledger.rows["a"].update({"kind": "ops", "branch": "", "state": "claimed"})


@pytest.mark.parametrize("kind", [{}, {"kind": "ci"}, {"kind": "plan"}])
def test_park_refuses_a_branchless_code_ci_or_plan_dependency_beside_a_branchless_ops_one(parked, capsys, kind):
    _branchless_ops(parked[1])
    parked[1].rows["c"] = {"id": "c", "title": "Ship the queue", "state": "claimed", "lane": "eng", **kind}
    parked[1].rows["t1"]["depends_on"] = ["a", "b", "c"]
    _refused(parked, capsys, "no open dependency records a branch, so there is nothing to stack on")


@pytest.mark.parametrize("kind", ["ops", "tune", "troubleshoot", "research"])
def test_park_stacks_on_dev_when_every_open_dependency_is_a_branchless_non_code_task(parked, capsys, kind):
    _, ledger, _, shell, doc = parked
    _branchless_ops(ledger)
    ledger.rows["a"]["kind"] = kind
    assert park(doc) == 0
    assert ("t1", {"parked_on": ["a"], "stacked_base": BASE, "parked_repos": []}, AGENT) in ledger.updates
    body = f"Parked on branch `eng-t1` until a is done. Stacked base `{BASE}`. {NOTE}"
    assert shell.calls == [
        ["git", "ls-remote", "--heads", "origin", "eng-t1"],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain"],
        *LOCATE,
        ["git", "fetch", "origin", "dev"],
        ["git", "merge-base", "HEAD", "origin/dev"],
        ["git", "rev-list", "--count", BASE],
        ["gh", "issue", "comment", ISSUE, "--body", body],
        REMOVE,
    ]
    assert json.loads(capsys.readouterr().out)["stacked_base"] == BASE


def test_a_task_parked_on_a_branchless_ops_dependency_waits_then_goes_to_the_next_engineer(parked):
    store, ledger, rt, _, doc = parked
    _branchless_ops(ledger)
    text = doc.read_text()
    assert park(doc) == 0
    assert store.handoff("sw", "t1") == text
    tick._reap("sw", store, ledger, rt, ledger.rows, 1)
    assert ledger.rows["t1"]["state"] == "open" and ledger.rows["t1"]["parked_on"] == ["a"]
    assert not task_dependencies.ready(ledger.rows["t1"], ledger.rows)
    assert claim_hold.waits(ledger.rows["t1"], ledger.rows) == "it waits on task a"
    ledger.rows["a"]["state"] = "done"
    ledger.update_task = type(ledger).update_task.__get__(ledger)
    before = len(rt.tasks)
    tick.tick("sw", store, ledger, rt, now_ms=NOW + 1)
    [launched] = [task for task in rt.tasks[before:] if task["id"] == "t1"]
    assert (launched["parked_on"], launched["stacked_base"], launched["stack_base"]) == (["a"], BASE, [])
    assert launched["handoff"] == text
    assert ledger.rows["t1"]["state"] == "claimed" and store.claimant("sw", "t1") != AGENT


def test_park_refuses_a_malformed_document(parked, capsys):
    doc = parked[4]
    doc.write_text(doc.read_text().replace("<!-- handoff complete -->", ""))
    found = handoff_check.problems(doc.read_text(), lambda address: True)
    _refused(parked, capsys, handoff_check.refusal(found))


def test_park_refuses_a_missing_document(parked, capsys, tmp_path):
    missing = tmp_path / "gone.md"
    _refused((*parked[:4], missing), capsys, f"cannot read the handoff document {missing}: No such file or directory")


@pytest.mark.parametrize("address", ["recap:eng-1@sw", "workspace:t1/progress"])
def test_park_resolves_read_first_addresses_against_this_swarm(parked, capsys, address):
    doc = parked[4]
    doc.write_text(doc.read_text().replace(address, address.replace("1", "9")))
    assert park(doc) == 1
    assert "does not resolve" in capsys.readouterr().err


def test_park_writes_the_open_dependencies_and_the_stacked_base(parked, capsys):
    _, ledger, _, shell, doc = parked
    assert park(doc) == 0
    assert ("t1", {"parked_on": ["a"], "stacked_base": BASE, "parked_repos": []}, AGENT) in ledger.updates
    assert ledger.rows["t1"]["parked_on"] == ["a"] and ledger.rows["t1"]["stacked_base"] == BASE
    body = f"Parked on branch `eng-t1` until a (`eng-a`) merges. Stacked base `{BASE}`. {NOTE}"
    assert shell.calls == [
        ["git", "ls-remote", "--heads", "origin", "eng-t1"],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain"],
        *LOCATE,
        ["git", "fetch", "origin", "eng-a"],
        ["git", "merge-base", "HEAD", "origin/eng-a"],
        ["git", "rev-list", "--count", BASE],
        ["gh", "issue", "comment", ISSUE, "--body", body],
        REMOVE,
    ]
    assert json.loads(capsys.readouterr().out) == {
        "task": "t1",
        "parked_on": ["a"],
        "stacked_base": BASE,
        "parked_repos": [],
        "worktree_removed": TOP,
        "next": "stop now; the task waits on its branch",
    }


def test_a_read_only_task_releases_its_seat_without_a_branch_or_worktree(parked, capsys):
    store, ledger, _, shell, doc = parked
    ledger.rows["t1"].update({"kind": "tune", "branch": "", "issue_url": ""})
    assert park(doc) == 0
    assert ("t1", {"parked_on": ["a"]}, AGENT) in ledger.updates
    assert ledger.rows["t1"]["parked_on"] == ["a"] and "stacked_base" not in ledger.rows["t1"]
    assert shell.calls == []
    assert ("sw", "t1", stack.READ_ONLY_NOTE.format(names="Build the parser"), AGENT) in ledger.comments
    assert store.handoff("sw", "t1") == doc.read_text()
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["finished"]
    assert json.loads(capsys.readouterr().out) == {
        "task": "t1",
        "parked_on": ["a"],
        "worktree_removed": "",
        "next": "stop now; the task waits on its dependencies and the next engineer continues it",
    }


def test_a_read_only_task_with_no_open_dependency_is_refused(parked, capsys):
    ledger = parked[1]
    ledger.rows["t1"].update({"kind": "tune", "branch": ""})
    ledger.rows["a"]["state"] = "done"
    _refused(parked, capsys, "the task waits on no open dependency; finish it, or hand it off for its window")


def test_a_read_only_task_names_each_open_dependency_including_one_missing_from_the_ledger(parked):
    _, ledger, _, _, doc = parked
    ledger.rows["t1"].update({"kind": "tune", "branch": "", "depends_on": ["a", "gone"]})
    assert park(doc) == 0
    assert ledger.rows["t1"]["parked_on"] == ["a", "gone"]
    note = stack.READ_ONLY_NOTE.format(names="Build the parser and its dependency")
    assert ("sw", "t1", note, AGENT) in ledger.comments


def test_a_read_only_task_names_a_title_the_comment_filter_refuses_only_as_its_dependencies(parked):
    _, ledger, _, _, doc = parked
    ledger.rows["t1"].update({"kind": "tune", "branch": ""})
    ledger.rows["a"]["title"] = "Fix the parser — then the schema"
    assert park(doc) == 0
    assert ("sw", "t1", stack.READ_ONLY_NOTE.format(names="its dependencies"), AGENT) in ledger.comments


def test_a_read_only_task_refuses_a_malformed_document(parked, capsys):
    parked[1].rows["t1"].update({"kind": "tune", "branch": ""})
    doc = parked[4]
    doc.write_text(doc.read_text().replace("<!-- handoff complete -->", ""))
    found = handoff_check.problems(doc.read_text(), lambda address: True)
    _refused(parked, capsys, handoff_check.refusal(found))


def test_a_doctor_tune_task_parks_on_a_merged_fix_not_yet_applied(parked, monkeypatch):
    store, ledger, _, _, doc = parked
    config = store.config
    monkeypatch.setattr(store, "config", lambda slug: dataclasses.replace(config(slug), template="doctor"))
    ledger.rows["t1"].update({"kind": "tune", "branch": "", "depends_on": ["a"]})
    ledger.rows["a"].update({"state": "done", "kind": "code"})
    assert park(doc) == 0
    assert ledger.rows["t1"]["parked_on"] == ["a"]


@pytest.mark.parametrize("repo", [None, "https://github.com/o/other"])
def test_park_accepts_an_open_dependency_merged_into_dev_after_its_branch_is_deleted(parked, monkeypatch, repo):
    _, ledger, _, shell, doc = parked
    dependency_pr = "https://github.com/o/r/pull/42"
    ledger.rows["a"].update({"pr_url": dependency_pr, "branch_repo": repo})

    def merged_dependency(argv):
        result = shell(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"state": "MERGED", "baseRefName": "dev"}), "")
        if argv == ["git", "fetch", "origin", "eng-a"]:
            return subprocess.CompletedProcess(argv, 1, "", "remote branch deleted")
        return result

    monkeypatch.setattr(stack, "shell", merged_dependency)
    assert park(doc) == 0
    assert ledger.rows["a"]["state"] == "pr"
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert ledger.rows["t1"]["stacked_base"] == BASE
    assert ["git", "fetch", "origin", "dev"] in shell.calls
    assert ["git", "merge-base", "HEAD", "origin/dev"] in shell.calls
    assert ["git", "fetch", "origin", "eng-a"] not in shell.calls
    assert ["git", "fetch", "--", repo, "eng-a"] not in shell.calls
    assert ["gh", "pr", "view", dependency_pr, "--json", "state,baseRefName"] in shell.calls


@pytest.mark.parametrize(
    "pull",
    [
        {"state": "OPEN", "baseRefName": "dev"},
        {"state": "CLOSED", "baseRefName": "dev"},
        {"state": "MERGED", "baseRefName": "main"},
    ],
)
def test_park_keeps_the_dependency_branch_until_its_pull_request_merges_into_dev(parked, monkeypatch, pull):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["pr_url"] = "https://github.com/o/r/pull/42"

    def dependency_pull(argv):
        result = shell(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(pull), "")
        return result

    monkeypatch.setattr(stack, "shell", dependency_pull)
    assert park(doc) == 0
    assert ["git", "fetch", "origin", "eng-a"] in shell.calls
    assert ["git", "merge-base", "HEAD", "origin/eng-a"] in shell.calls
    assert ["git", "fetch", "origin", "dev"] not in shell.calls


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ("not json", "Expecting value: line 1 column 1 (char 0)"),
        ("{}", "'state'"),
        ('{"state": "MERGED"}', "'baseRefName'"),
    ],
)
def test_park_refuses_unreadable_dependency_pull_request_state(parked, monkeypatch, capsys, answer, error):
    _, ledger, _, shell, _ = parked
    ledger.rows["a"]["pr_url"] = "https://github.com/o/r/pull/42"

    def unreadable_pull(argv):
        result = shell(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, answer, "")
        return result

    monkeypatch.setattr(stack, "shell", unreadable_pull)
    _refused(parked, capsys, f"cannot read the dependency pull request: {error}")
    assert ["git", "fetch", "origin", "dev"] not in shell.calls


def test_park_refuses_when_the_dependency_pull_request_cannot_be_read(parked, capsys):
    _, ledger, _, shell, _ = parked
    ledger.rows["a"]["pr_url"] = "https://github.com/o/r/pull/42"
    shell.fail = (("gh", "pr", "view"),)
    _refused(parked, capsys, "cannot read the dependency pull request: boom")


@pytest.mark.parametrize("merged_before_stop", [False, True])
def test_a_stop_after_park_passes_the_claim_stop_gate(parked, tmp_path, merged_before_stop):
    store, ledger, _, _, doc = parked
    ledger.rows["t1"].update({"state": "claimed", "claimed_by": AGENT, "pr_url": ""})
    gate = ClaimStop(connect=lambda: store, ledger=lambda: ledger, github=lambda url: None, now=lambda: NOW)
    who = Who(name=AGENT, swarm="sw", lane="eng", task="t1")

    def stop():
        return gate.decide(Call(""), who, Verdicts("sw", gate.name, tmp_path))

    assert not stop().allowed
    assert park(doc) == 0
    if merged_before_stop:
        ledger.rows["a"]["state"] = "done"
    assert stop() == Decision()
    assert ledger.rows["t1"]["state"] == "claimed"


def test_park_removes_the_worktree_only_after_the_seat_is_handed_off(parked, monkeypatch):
    store, _, _, shell, doc = parked

    def answer(argv):
        if argv == REMOVE:
            assert store.handoff("sw", "t1") == doc.read_text()
            assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["finished"]
        return shell(argv)

    monkeypatch.setattr(stack, "shell", answer)
    assert park(doc) == 0
    assert shell.calls[-1] == REMOVE


def test_park_reports_a_worktree_it_could_not_remove_after_parking(parked, capsys):
    store, ledger, _, shell, doc = parked
    shell.fail = (tuple(REMOVE[:3]),)
    assert park(doc) == 1
    assert capsys.readouterr().err == (
        f"swarm: the task is parked and its seat handed off, but wt.sh done could not remove {TOP}: boom\n"
    )
    calls = len(shell.calls)
    assert park(doc) == 1
    assert capsys.readouterr().err == (
        f"swarm: {AGENT} already handed off its seat; remove a leftover worktree with wt.sh done\n"
    )
    assert len(shell.calls) == calls and len(transfers.list_transfers(store, "sw")) == 1
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert store.handoff("sw", "t1") == doc.read_text()


def test_the_worktree_script_ships_with_the_package():
    assert stack.WT_SCRIPT.is_file()
    assert stack.WT_SCRIPT.parts[-5:] == ("package", "skills", "worktree", "scripts", "wt.sh")


def test_park_comments_the_ledger_task_naming_the_blocker_in_plain_words(parked):
    _, ledger, _, _, doc = parked
    assert park(doc) == 0
    assert ledger.comments == [("sw", "t1", f"Parked on its branch until Build the parser is done. {NOTE}", AGENT)]


def test_park_stacks_on_the_deepest_of_several_dependency_branches(parked, monkeypatch):
    _, ledger, _, shell, doc = parked
    ledger.rows["b"]["state"] = "claimed"
    ledger.rows["c"] = {"id": "c", "title": "Ship it", "state": "pr", "done": True, "branch": "eng-c"}
    ledger.rows["t1"]["depends_on"] = ["a", "b", "gone", "c"]
    bases = {"origin/eng-a": "2" * 40, "origin/eng-b": "1" * 40}
    counts = {"2" * 40: "4", "1" * 40: "9"}

    def answer(argv):
        if argv[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(argv, 0, stdout=bases[argv[-1]] + "\n", stderr="")
        if argv[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=counts[argv[-1]] + "\n", stderr="")
        return shell(argv)

    monkeypatch.setattr(stack, "shell", answer)
    assert park(doc) == 0
    assert ledger.rows["t1"]["parked_on"] == ["a", "b", "gone"]
    assert ledger.rows["t1"]["stacked_base"] == "1" * 40
    [comment] = shell.comments()
    body = f"Parked on branch `eng-t1` until a (`eng-a`), b (`eng-b`), gone merges. Stacked base `{'1' * 40}`. {NOTE}"
    assert comment[-1] == body
    note = f"Parked on its branch until Build the parser and Write the schema and its dependency is done. {NOTE}"
    assert ledger.comments == [("sw", "t1", note, AGENT)]


OTHER = "https://github.com/o/bundle"


def test_park_fetches_a_dependency_branch_from_its_other_repository(parked, capsys):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch_repo"] = OTHER
    assert park(doc) == 0
    fields = {"parked_on": ["a"], "stacked_base": BASE, "parked_repos": [OTHER]}
    assert ("t1", fields, AGENT) in ledger.updates
    assert ledger.rows["t1"]["parked_repos"] == [OTHER]
    body = f"Parked on branch `eng-t1` until a (`eng-a` in {OTHER}) merges. Stacked base `{BASE}`. {NOTE}"
    assert shell.calls == [
        ["git", "ls-remote", "--heads", "origin", "eng-t1"],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--porcelain"],
        *LOCATE,
        ["git", "remote", "get-url", "origin"],
        ["git", "fetch", "--", OTHER, "eng-a"],
        ["git", "fetch", "origin", "dev"],
        ["git", "merge-base", "HEAD", "origin/dev"],
        ["git", "rev-list", "--count", BASE],
        ["gh", "issue", "comment", ISSUE, "--body", body],
        REMOVE,
    ]
    assert json.loads(capsys.readouterr().out)["parked_repos"] == [OTHER]


@pytest.mark.parametrize(
    "repo", ["git@github.com:o/r.git", "https://github.com/O/R/", "ssh://git@github.com/o/r", "https://github.com/o/r"]
)
def test_park_fetches_a_dependency_in_the_same_repository_from_origin(parked, repo):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch_repo"] = repo
    assert park(doc) == 0
    assert ("t1", {"parked_on": ["a"], "stacked_base": BASE, "parked_repos": []}, AGENT) in ledger.updates
    assert ledger.rows["t1"]["parked_repos"] == []
    assert ["git", "remote", "get-url", "origin"] in shell.calls
    assert ["git", "fetch", "origin", "eng-a"] in shell.calls
    assert ["git", "merge-base", "HEAD", "origin/eng-a"] in shell.calls
    [comment] = shell.comments()
    assert "a (`eng-a`) merges" in comment[-1]


def test_park_names_each_other_repository_once_in_dependency_order(parked):
    _, ledger, _, shell, doc = parked
    ledger.rows["b"].update({"state": "claimed", "branch_repo": "git@github.com:O/bundle.git"})
    ledger.rows["c"] = {"id": "c", "title": "Docs", "state": "pr", "branch": "eng-c", "branch_repo": "/srv/docs.git"}
    ledger.rows["d"] = {"id": "d", "title": "Plan", "state": "pr", "branch": "", "branch_repo": "/srv/plan.git"}
    ledger.rows["a"]["branch_repo"] = OTHER
    ledger.rows["t1"]["depends_on"] = ["a", "b", "c", "d"]
    assert park(doc) == 0
    assert ledger.rows["t1"]["parked_on"] == ["a", "b", "c", "d"]
    assert ledger.rows["t1"]["parked_repos"] == [OTHER, "/srv/docs.git"]
    assert shell.calls.count(["git", "remote", "get-url", "origin"]) == 1
    assert [c[3:] for c in shell.calls if c[:3] == ["git", "fetch", "--"]] == [
        [OTHER, "eng-a"],
        ["git@github.com:O/bundle.git", "eng-b"],
        ["/srv/docs.git", "eng-c"],
    ]


def test_park_reads_no_origin_url_when_no_dependency_records_a_repository(parked):
    _, ledger, _, shell, doc = parked
    ledger.rows["b"].update({"state": "claimed", "branch": "", "branch_repo": OTHER})
    assert park(doc) == 0
    assert ["git", "remote", "get-url", "origin"] not in shell.calls
    assert ledger.rows["t1"]["parked_repos"] == []


ANTONCORE = "https://github.com/o/antoncore"
CHECKOUT = "/home/me/dev/antoncore"


def _antoncore_dependency(parked, monkeypatch, origin):
    _, ledger, _, shell, _ = parked
    ledger.rows["a"].update({"branch_repo": ANTONCORE, "repository_branches": {CHECKOUT: "eng-a"}})

    def answer(argv):
        if argv == ["git", "-C", CHECKOUT, "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{origin}\n", stderr="")
        return shell(argv)

    monkeypatch.setattr(stack, "shell", answer)


def test_park_fetches_an_antoncore_dependency_through_the_antoncore_checkout(parked, monkeypatch):
    _, ledger, _, shell, doc = parked
    _antoncore_dependency(parked, monkeypatch, "git@github.com:O/antoncore.git")
    assert park(doc) == 0
    assert ["git", "-C", CHECKOUT, "fetch", "origin", "eng-a"] in shell.calls
    assert [c for c in shell.calls if c[:3] == ["git", "fetch", "--"]] == []
    assert ledger.rows["t1"]["parked_repos"] == [ANTONCORE]


@pytest.mark.parametrize("origin", ["git@github.com:o/bundle.git", ""])
def test_park_fetches_by_url_when_the_recorded_checkout_has_no_matching_origin(parked, monkeypatch, origin):
    _, ledger, _, shell, doc = parked
    _antoncore_dependency(parked, monkeypatch, origin)
    assert park(doc) == 0
    assert ["git", "fetch", "--", ANTONCORE, "eng-a"] in shell.calls
    assert [c for c in shell.calls if c[:2] == ["git", "-C"]] == []


def test_park_refuses_when_the_antoncore_checkout_cannot_fetch(parked, monkeypatch, capsys):
    _antoncore_dependency(parked, monkeypatch, "https://github.com/o/antoncore.git")
    parked[3].fail = (("git", "-C", CHECKOUT),)
    _refused(parked, capsys, f"cannot fetch eng-a in {CHECKOUT}: boom")


UNVERIFIED = "Host key verification failed.\nfatal: Could not read from remote repository."
KEY_REFUSED = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository."
PROMPTLESS = ["env", "GIT_TERMINAL_PROMPT=0"]


def _ssh_refused(shell, refused, inner=None, stderr=UNVERIFIED):
    def answer(argv):
        if argv in refused:
            shell.calls.append(argv)
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr=stderr)
        return (inner or shell)(argv)

    return answer


@pytest.mark.parametrize("stderr", [UNVERIFIED, KEY_REFUSED])
def test_park_fetches_an_ssh_dependency_over_https_when_the_host_key_cannot_be_verified(parked, monkeypatch, stderr):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch_repo"] = "git@github.com:O/bundle.git"
    refused = [["git", "fetch", "--", "git@github.com:O/bundle.git", "eng-a"]]
    monkeypatch.setattr(stack, "shell", _ssh_refused(shell, refused, stderr=stderr))
    assert park(doc) == 0
    assert [*PROMPTLESS, "git", "fetch", "--", "https://github.com/O/bundle.git", "eng-a"] in shell.calls
    assert ledger.rows["t1"]["parked_on"] == ["a"]


def test_park_fetches_a_same_repository_dependency_over_https_when_origin_is_unverified(parked, monkeypatch):
    _, ledger, _, shell, doc = parked
    monkeypatch.setattr(stack, "shell", _ssh_refused(shell, [["git", "fetch", "origin", "eng-a"]]))
    assert park(doc) == 0
    refspec = "+refs/heads/eng-a:refs/remotes/origin/eng-a"
    assert [*PROMPTLESS, "git", "fetch", "--", "https://github.com/O/r.git", refspec] in shell.calls
    assert ["git", "merge-base", "HEAD", "origin/eng-a"] in shell.calls


def test_park_fetches_through_an_unverified_checkout_origin_over_https(parked, monkeypatch):
    _, ledger, _, shell, doc = parked
    _antoncore_dependency(parked, monkeypatch, "git@github.com:O/antoncore.git")
    refused = [["git", "-C", CHECKOUT, "fetch", "origin", "eng-a"]]
    monkeypatch.setattr(stack, "shell", _ssh_refused(shell, refused, stack.shell))
    assert park(doc) == 0
    refspec = "+refs/heads/eng-a:refs/remotes/origin/eng-a"
    assert [
        *PROMPTLESS,
        "git",
        "-C",
        CHECKOUT,
        "fetch",
        "--",
        "https://github.com/O/antoncore.git",
        refspec,
    ] in shell.calls


def test_park_refuses_naming_both_errors_when_the_https_retry_fails(parked, monkeypatch, capsys):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch_repo"] = "ssh://git@github.com:22/O/bundle"
    https = [*PROMPTLESS, "git", "fetch", "--", "https://github.com/O/bundle", "eng-a"]
    monkeypatch.setattr(
        stack, "shell", _ssh_refused(shell, [["git", "fetch", "--", "ssh://git@github.com:22/O/bundle", "eng-a"]])
    )
    shell.fail = (tuple(https[:3]),)
    assert park(doc) == 1
    says = (
        "swarm: cannot fetch eng-a from ssh://git@github.com:22/O/bundle: "
        f"{UNVERIFIED}; over https://github.com/O/bundle: boom\n"
    )
    assert capsys.readouterr().err == says
    assert https in shell.calls
    assert "parked_on" not in ledger.rows["t1"]


def test_park_names_the_ssh_error_when_the_https_retry_cannot_run(parked, monkeypatch, capsys):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch_repo"] = "git@github.com:O/bundle.git"
    inner = _ssh_refused(shell, [["git", "fetch", "--", "git@github.com:O/bundle.git", "eng-a"]])

    def answer(argv):
        if argv[:2] == PROMPTLESS:
            raise SwarmError("env GIT_TERMINAL_PROMPT=0 could not run: timed out")
        return inner(argv)

    monkeypatch.setattr(stack, "shell", answer)
    assert park(doc) == 1
    says = (
        f"swarm: cannot fetch eng-a from git@github.com:O/bundle.git: {UNVERIFIED}; "
        "over https://github.com/O/bundle.git: env GIT_TERMINAL_PROMPT=0 could not run: timed out\n"
    )
    assert capsys.readouterr().err == says
    assert "parked_on" not in ledger.rows["t1"]


def test_park_names_the_ssh_error_when_the_origin_url_cannot_be_read(parked, monkeypatch, capsys):
    _, ledger, _, shell, doc = parked
    inner = _ssh_refused(shell, [["git", "fetch", "origin", "eng-a"]])

    def answer(argv):
        if argv == ["git", "remote", "get-url", "origin"]:
            raise SwarmError("git remote could not run: timed out")
        return inner(argv)

    monkeypatch.setattr(stack, "shell", answer)
    assert park(doc) == 1
    assert capsys.readouterr().err == f"swarm: cannot fetch eng-a: {UNVERIFIED}; git remote could not run: timed out\n"
    assert [c for c in shell.calls if c[:2] == PROMPTLESS] == []


def test_park_refuses_an_unverified_fetch_from_an_address_with_no_https_form(parked, monkeypatch, capsys):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch_repo"] = OTHER
    monkeypatch.setattr(stack, "shell", _ssh_refused(shell, [["git", "fetch", "--", OTHER, "eng-a"]]))
    assert park(doc) == 1
    assert capsys.readouterr().err == f"swarm: cannot fetch eng-a from {OTHER}: {UNVERIFIED}\n"
    assert [c for c in shell.calls if c[:2] == PROMPTLESS] == []


def test_park_never_retries_over_https_when_the_ssh_failure_is_not_verification(parked, capsys):
    _, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch_repo"] = "git@github.com:O/bundle.git"
    shell.fail = (("git", "fetch", "--"),)
    _refused(parked, capsys, "cannot fetch eng-a from git@github.com:O/bundle.git: boom")
    assert [c[3] for c in shell.calls if c[:3] == ["git", "fetch", "--"]] == ["git@github.com:O/bundle.git"]


@pytest.mark.parametrize(
    ("url", "https"),
    [
        ("git@github.com:O/bundle.git", "https://github.com/O/bundle.git"),
        ("ssh://git@github.com/O/bundle", "https://github.com/O/bundle"),
        ("ssh://git@github.com:22/O/bundle.git", "https://github.com/O/bundle.git"),
        ("https://github.com/O/bundle", ""),
        ("/srv/repos/docs.git", ""),
        ("./a:b/docs.git", ""),
        ("file:///srv/docs.git", ""),
        ("git+ssh://git@github.com/O/bundle", "https://github.com/O/bundle"),
        ("ssh://git@github.com/O/r://x", "https://github.com/O/r://x"),
        ("git@github.com:O/a:b.git", "https://github.com/O/a:b.git"),
        ("git@github.com:/O/bundle.git/", "https://github.com/O/bundle.git/"),
        ("deploy@ops@github.com:O/bundle.git", "https://github.com/O/bundle.git"),
        ("ssh://deploy@ops@github.com/O/bundle", "https://github.com/O/bundle"),
        ("git@github.com:Xen/bundle.git", "https://github.com/Xen/bundle.git"),
        ("ssh://git@github.com", ""),
        ("git@github.com:", ""),
    ],
)
def test_https_url_maps_only_ssh_addresses(url, https):
    assert stack.https_url(url) == https


LONG = "Teach the ledger server to keep every agent status comment short enough for the operator to read at once"


def test_park_keeps_the_ledger_note_within_the_comment_limit_for_three_long_titled_dependencies(parked):
    _, ledger, _, _, doc = parked
    for name in ("a", "b", "c"):
        ledger.rows[name] = {"id": name, "title": f"{LONG} part {name}", "state": "pr", "branch": f"eng-{name}"}
    ledger.rows["t1"]["depends_on"] = ["a", "b", "c"]
    assert park(doc) == 0
    note = f"Parked on its branch until every open dependency is done. {NOTE}"
    assert ledger.comments == [("sw", "t1", note, AGENT)]
    assert ledger_comments.problems(note, "comment") == []


def test_park_names_a_title_the_comment_filter_refuses_only_as_an_open_dependency(parked):
    _, ledger, _, _, doc = parked
    ledger.rows["a"]["title"] = "Fix the parser — then the schema"
    assert park(doc) == 0
    assert ledger.comments == [("sw", "t1", f"Parked on its branch until every open dependency is done. {NOTE}", AGENT)]


def test_park_writes_the_ledger_note_before_the_issue_comment_and_the_task_update(parked, monkeypatch, capsys):
    store, ledger, _, shell, doc = parked
    note = ("sw", "t1", f"Parked on its branch until Build the parser is done. {NOTE}", AGENT)
    seen = []

    def answer(argv):
        if argv[:3] == ["gh", "issue", "comment"]:
            seen.append((list(ledger.comments), list(ledger.updates)))
        return shell(argv)

    def amend(slug, task, text, by):
        if ledger.comments[-1:] != [(slug, task, text, by)]:
            ledger.comments.append((slug, task, text, by))

    monkeypatch.setattr(stack, "shell", answer)
    ledger.comment = amend
    shell.fail = (("gh", "issue", "comment"),)
    assert park(doc) == 1
    assert capsys.readouterr().err == "swarm: could not comment on the issue: boom\n"
    assert seen == [([note], [])]
    assert "parked_on" not in ledger.rows["t1"] and REMOVE not in shell.calls
    assert store.handoff("sw", "t1") == ""
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["working"]
    shell.fail = ()
    assert park(doc) == 0
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert ledger.comments == [note]


@pytest.mark.parametrize(
    ("failing", "says"),
    [
        (("git", "remote", "get-url"), "cannot read the origin url"),
        (("git", "fetch", "--"), f"cannot fetch eng-a from {OTHER}"),
        (("git", "fetch", "origin"), "cannot fetch dev"),
        (("git", "merge-base", "HEAD"), "the task shares no history with dev"),
    ],
)
def test_park_on_another_repository_refuses_when_a_command_fails(parked, capsys, failing, says):
    parked[1].rows["a"]["branch_repo"] = OTHER
    parked[3].fail = (failing,)
    _refused(parked, capsys, f"{says}: boom")


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("git@github.com:The-Cloud-Clockwork/agentihooks.git", "github.com/the-cloud-clockwork/agentihooks"),
        ("https://github.com/the-cloud-clockwork/agentihooks", "github.com/the-cloud-clockwork/agentihooks"),
        ("ssh://git@github.com/o/r.git/", "github.com/o/r"),
        ("/srv/repos/docs.git", "/srv/repos/docs"),
        ("/srv/repos/docs.git/", "/srv/repos/docs"),
        ("/srv/Repos/Docs.git", "/srv/Repos/Docs"),
        ("https://github.com/o/gist.git", "github.com/o/gist"),
        ("git@github.com:o/digit", "github.com/o/digit"),
        ("https://github.com/o/r?u=ssh://x", "github.com/o/r?u=ssh://x"),
        ("/srv/A:b/Repo.git", "/srv/A:b/Repo"),
        ("/srv/REPOX/", "/srv/REPOX"),
        ("https://github.com/o@x/r", "github.com/o@x/r"),
        ("git@host:a:b/r", "host/a:b/r"),
        ("https://github.com/o/REPOX", "github.com/o/repox"),
    ],
)
def test_a_repository_key_ignores_scheme_user_case_and_suffix(url, key):
    assert stack.repo_key(url) == key


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://user:token@github.com/o/r.git", "https://github.com/o/r.git"),
        ("HTTPS://token@github.com/o/r", "HTTPS://github.com/o/r"),
        ("ssh://git@github.com/o/r.git", "ssh://git@github.com/o/r.git"),
        ("ssh://git:secret@host/o/r", "ssh://git@host/o/r"),
        ("ssh://host/o/r", "ssh://host/o/r"),
        ("https://user:p@ss@github.com/o/r", "https://github.com/o/r"),
        ("https://github.com/o@x/r?next=https://h", "https://github.com/o@x/r?next=https://h"),
        ("https://user:tok@github.com/o?next=https://h", "https://github.com/o?next=https://h"),
        ("https://github.com/o/r", "https://github.com/o/r"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("/srv/repos/docs.git", "/srv/repos/docs.git"),
    ],
)
def test_a_public_url_drops_credentials_from_a_scheme_url(url, public):
    assert stack.public_url(url) == public


def test_park_without_issues_skips_the_issue_comment(parked):
    _, ledger, _, shell, doc = parked
    ledger.rows["t1"]["issue_url"] = ""
    shell.issues = json.dumps({"hasIssuesEnabled": False})
    assert park(doc) == 0
    assert shell.comments() == [] and ledger.rows["t1"]["parked_on"] == ["a"]
    assert ["gh", "repo", "view", "--json", "hasIssuesEnabled"] in shell.calls


def test_park_stores_the_handoff_and_retires_the_agent_so_the_reaped_task_waits(parked):
    store, ledger, rt, _, doc = parked
    text = doc.read_text()
    assert park(doc) == 0
    assert store.handoff("sw", "t1") == text
    assert store.handoff_envelope("sw", "t1")["reason"] == "exit"
    [transfer] = transfers.list_transfers(store, "sw")
    assert (transfer["reason"], transfer["at"], transfer["handoff"]) == ("exit", NOW, text)
    recap = "\n\n".join(f"## {h}\n{handoff_check.section(text, h)}" for h in ("Done", "Stopped at", "Next"))
    assert store.memory.recaps("eng-1@sw")[0] == {"occupant": AGENT, "task": "t1", "text": recap, "at": NOW}
    assert SeatRegistry(store.redis).exit_of(AGENT) == {
        "seat": "eng-1@sw",
        "reason": "handed off its seat",
        "generation": 1,
    }
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["finished"]
    assert store.claimant("sw", "t1") == AGENT
    tick._reap("sw", store, ledger, rt, ledger.rows, 1)
    assert [a.name for a in store.agents("sw") if a.name == AGENT] == []
    assert ledger.rows["t1"]["state"] == "open" and ledger.rows["t1"]["parked_on"] == ["a"]
    assert store.handoff("sw", "t1") == text


def test_park_marks_the_next_start_as_after_a_park(parked, capsys):
    store, _, _, _, doc = parked
    assert store.take_restart("sw", "t1") == ""
    assert park(doc) == 0
    assert store.take_restart("sw", "t1") == "park"


def test_a_refused_park_leaves_no_restart_mark(parked, capsys):
    store, ledger, _, _, doc = parked
    del ledger.rows["t1"]["branch"]
    assert park(doc) == 1
    assert store.take_restart("sw", "t1") == ""


def test_the_life_launched_after_a_park_is_recorded_as_after_the_park(parked):
    store, ledger, rt, _, doc = parked
    assert park(doc) == 0
    tick._reap("sw", store, ledger, rt, ledger.rows, 1)
    ledger.rows["a"]["state"] = "done"
    ledger.update_task = type(ledger).update_task.__get__(ledger)
    tick.tick("sw", store, ledger, rt, now_ms=NOW + 1)
    lives = sorted(
        (row["agent"] == AGENT, row["state"], row["after"]) for row in store.launches("sw") if row["task"] == "t1"
    )
    assert lives == [(False, "started", "park"), (True, "started", "")]


def test_shell_runs_with_captured_text_and_a_timeout(monkeypatch):
    seen = []
    done = subprocess.CompletedProcess(["git", "status"], 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(stack.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)) or done)
    assert stack.shell(["git", "status"]) is done
    assert seen == [(["git", "status"], {"capture_output": True, "text": True, "timeout": 60})]


def test_shell_names_a_command_that_could_not_run(monkeypatch):
    def broken(argv, **kw):
        raise OSError("no such program")

    monkeypatch.setattr(stack.subprocess, "run", broken)
    with pytest.raises(SwarmError, match=r"^git status could not run: no such program$"):
        stack.shell(["git", "status", "--short"])


def _git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def stacked_repo(parked, monkeypatch, tmp_path):
    store, ledger, rt, _, doc = parked
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "dev", str(repo))
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "Initial")
    _git(repo, "push", "-u", "origin", "dev")
    _git(repo, "switch", "-c", "blocker")
    (repo / "blocker.txt").write_text("first\n")
    _git(repo, "add", "blocker.txt")
    _git(repo, "commit", "-m", "Blocker first")
    (repo / "blocker.txt").write_text("second\n")
    _git(repo, "commit", "-am", "Blocker second")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-c", "parked")
    (repo / "shared.txt").write_text("task work\n")
    _git(repo, "commit", "-am", "Task work")
    _git(repo, "push", "origin", "parked")
    _git(repo, "switch", "dev")
    _git(repo, "merge", "--squash", "blocker")
    _git(repo, "commit", "-m", "Squash blocker")
    _git(repo, "push", "origin", "dev")
    _git(repo, "switch", "-c", "finisher", "parked")
    ledger.rows["a"]["state"] = "done"
    ledger.rows["t1"].update({"branch": "parked", "stacked_base": base, "parked_on": ["a"]})
    monkeypatch.chdir(repo)
    monkeypatch.setattr(stack, "shell", lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    return store, ledger, repo, base


def restack():
    return cli.main(["sw", "--as", AGENT, "restack"])


def test_restack_after_a_squash_merge_replays_only_task_work(stacked_repo, capsys):
    store, ledger, repo, base = stacked_repo
    agent = store.agents("sw")[0]
    before = _git(repo, "rev-parse", "HEAD")
    assert restack() == 0
    assert _git(repo, "rev-parse", "HEAD") != before
    assert _git(repo, "log", "--format=%s", "origin/dev..HEAD") == "Task work"
    assert _git(repo, "rev-parse", "HEAD^") == _git(repo, "rev-parse", "origin/dev")
    assert (repo / "blocker.txt").read_text() == "second\n"
    assert (repo / "shared.txt").read_text() == "task work\n"
    assert ledger.rows["t1"]["parked_on"] == []
    assert ledger.rows["t1"]["stacked_base"] == base
    assert (
        "t1",
        {
            "parked_on": [],
            "branch": "finisher",
            "branch_repo": str(repo.parent / "origin.git"),
            "repository_branches": {str(repo): "finisher"},
        },
        AGENT,
    ) in ledger.updates
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"task": "t1", "parked_on": []}
    assert store.agents("sw")[0] == agent
    assert not (repo / _git(repo, "rev-parse", "--git-path", "agentihooks-restack.json")).exists()


def test_restack_registers_the_successor_branch_before_returning(stacked_repo):
    _, ledger, repo, _ = stacked_repo
    ledger.rows["t1"]["repository_branches"] = {"/other/repository": "keep-other", str(repo): "parked"}
    assert restack() == 0
    task = ledger.rows["t1"]
    assert task["branch"] == "finisher"
    assert task["branch_repo"] == str(repo.parent / "origin.git")
    assert task["repository_branches"] == {"/other/repository": "keep-other", str(repo): "finisher"}
    assert task["parked_on"] == []


def test_restack_finds_a_parked_branch_kept_only_in_the_local_checkout(stacked_repo):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "push", "origin", "--delete", "parked")
    assert _git(repo, "ls-remote", "origin", "refs/heads/parked") == ""
    assert _git(repo, "for-each-ref", "refs/remotes/origin/parked") == ""
    assert restack() == 0
    assert _git(repo, "log", "--format=%s", "origin/dev..HEAD") == "Task work"
    assert _git(repo, "rev-parse", "HEAD^") == _git(repo, "rev-parse", "origin/dev")
    assert ledger.rows["t1"]["parked_on"] == []


def test_restack_refuses_when_the_remote_predecessor_has_work_missing_locally(stacked_repo, capsys):
    _, ledger, repo, _ = stacked_repo
    advanced = _git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "New predecessor work")
    _git(repo, "push", "origin", f"{advanced}:parked")
    before = _git(repo, "rev-parse", "HEAD")

    assert restack() == 1

    assert capsys.readouterr().err.endswith(
        "the stacked base is not in this branch; use a worktree cut from the parked branch\n"
    )
    assert _git(repo, "rev-parse", "HEAD") == before
    assert ledger.rows["t1"]["branch"] == "parked"
    assert ledger.updates == []


def test_restack_reports_checkout_lookup_failure_without_changing_task_metadata(stacked_repo, monkeypatch, capsys):
    _, ledger, _, _ = stacked_repo
    real_shell = stack.shell

    def failed(argv):
        if argv == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="git failure\n")
        return real_shell(argv)

    monkeypatch.setattr(stack, "shell", failed)

    assert restack() == 1
    assert capsys.readouterr().err == "swarm: cannot locate the worktree: git failure\n"
    assert ledger.rows["t1"]["branch"] == "parked"
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert ledger.updates == []


def test_restack_removes_stacked_commits_even_after_merging_dev(stacked_repo):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "fetch", "origin")
    _git(repo, "merge", "--no-edit", "origin/dev")
    assert "Blocker first" in _git(repo, "log", "--format=%s", "origin/dev..HEAD")
    assert restack() == 0
    assert _git(repo, "log", "--format=%s", "origin/dev..HEAD") == "Task work"
    assert ledger.rows["t1"]["parked_on"] == []


def test_restack_conflict_lists_files_and_keeps_parked_state(stacked_repo, capsys):
    _, ledger, repo, base = stacked_repo
    _git(repo, "switch", "dev")
    (repo / "shared.txt").write_text("integration work\n")
    _git(repo, "commit", "-am", "Integration work")
    _git(repo, "push", "origin", "dev")
    _git(repo, "switch", "finisher")
    capsys.readouterr()
    assert restack() == 1
    assert (
        "\nConflicted files:\nshared.txt\nResolve the files, run git rebase --continue, then run swarm restack again.\n"
        in capsys.readouterr().err
    )
    assert _git(repo, "diff", "--name-only", "--diff-filter=U") == "shared.txt"
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert ledger.rows["t1"]["stacked_base"] == base
    assert ledger.updates == []
    saved = json.loads((repo / _git(repo, "rev-parse", "--git-path", "agentihooks-restack.json")).read_text())
    assert saved == {"context": ["sw", "t1", "finisher", base], "onto": _git(repo, "rev-parse", "origin/dev")}


def _task_repo(tmp_path, monkeypatch):
    origin, repo = tmp_path / "origin.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "dev", str(repo))
    for key, value in (("user.email", "test@example.invalid"), ("user.name", "Test"), ("core.hooksPath", "/dev/null")):
        _git(repo, "config", key, value)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "shared.txt").write_text("original\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "Initial")
    _git(repo, "push", "-u", "origin", "dev")
    _git(repo, "switch", "-c", "parked")
    (repo / "task.txt").write_text("task work\n")
    _git(repo, "add", "task.txt")
    _git(repo, "commit", "-m", "Task work")
    _git(repo, "push", "origin", "parked")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(stack, "shell", lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=10))
    return repo, _git(repo, "rev-parse", "dev")


def test_the_stacked_base_of_another_repository_is_the_task_fork_from_dev(tmp_path, monkeypatch):
    other = tmp_path / "other"
    _git(tmp_path, "init", "-b", "eng-a", str(other))
    _git(other, "-c", "user.email=t@example.invalid", "-c", "user.name=T", "commit", "--allow-empty", "-m", "Other")
    repo, fork = _task_repo(tmp_path, monkeypatch)
    dep = {"id": "a", "branch": "eng-a", "branch_repo": str(other)}
    assert stack._foreign([dep]) == [dep]
    assert stack._stacked_base([dep], [dep]) == fork
    assert _git(repo, "cat-file", "-t", _git(other, "rev-parse", "eng-a")) == "commit"


def test_restack_of_a_task_parked_on_another_repository_rebases_onto_dev(parked, tmp_path, monkeypatch, capsys):
    ledger = parked[1]
    repo, fork = _task_repo(tmp_path, monkeypatch)
    _git(repo, "switch", "dev")
    (repo / "dev.txt").write_text("integration\n")
    _git(repo, "add", "dev.txt")
    _git(repo, "commit", "-m", "Integration work")
    _git(repo, "push", "origin", "dev")
    _git(repo, "switch", "-c", "finisher", "parked")
    gone = str(tmp_path / "gone.git")
    ledger.rows["a"].update({"state": "done", "branch_repo": gone})
    ledger.rows["t1"].update({"branch": "parked", "stacked_base": fork, "parked_on": ["a"], "parked_repos": [gone]})
    assert restack() == 0
    assert _git(repo, "log", "--format=%s", "origin/dev..HEAD") == "Task work"
    assert _git(repo, "rev-parse", "HEAD^") == _git(repo, "rev-parse", "origin/dev")
    assert (repo / "dev.txt").read_text() == "integration\n" and (repo / "task.txt").read_text() == "task work\n"
    assert ledger.rows["t1"]["parked_on"] == []
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"task": "t1", "parked_on": []}


def test_restack_fetches_origin_over_https_when_its_ssh_host_cannot_be_verified(stacked_repo, monkeypatch):
    _, ledger, repo, _ = stacked_repo
    calls = []
    real = stack.shell
    whole = "+refs/heads/*:refs/remotes/origin/*"

    def answer(argv):
        calls.append(argv)
        if argv == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr=UNVERIFIED)
        if argv == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(argv, 0, stdout="git@github.com:O/r.git\n", stderr="")
        if argv == [*PROMPTLESS, "git", "fetch", "--", "https://github.com/O/r.git", whole]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return real(argv)

    monkeypatch.setattr(stack, "shell", answer)
    assert restack() == 0
    assert [*PROMPTLESS, "git", "fetch", "--", "https://github.com/O/r.git", whole] in calls
    assert ledger.rows["t1"]["parked_on"] == []


def test_restack_reports_only_conflicted_files_when_other_work_changes(stacked_repo, monkeypatch, capsys):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "switch", "dev")
    (repo / "shared.txt").write_text("integration work\n")
    _git(repo, "commit", "-am", "Integration work")
    _git(repo, "push", "origin", "dev")
    _git(repo, "switch", "finisher")
    real_shell = stack.shell

    def concurrent_change(argv):
        done = real_shell(argv)
        if argv[1] == "rebase" and done.returncode != 0:
            (repo / "blocker.txt").write_text("unrelated work\n")
        return done

    monkeypatch.setattr(stack, "shell", concurrent_change)
    capsys.readouterr()
    assert restack() == 1
    err = capsys.readouterr().err
    assert err.partition("\nConflicted files:\n")[2].partition("\nResolve")[0] == "shared.txt"
    assert "blocker.txt" in _git(repo, "diff", "--name-only", "--diff-filter=u")
    assert (repo / "blocker.txt").read_text() == "unrelated work\n"
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert ledger.updates == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"parked_on": []}, "no parked work"),
        ({"stacked_base": ""}, "no parked work"),
        ({"parked_on": ["missing"]}, "unfinished parked dependency"),
        ({"parked_on": ["a", "missing"]}, "unfinished parked dependency"),
    ],
)
def test_restack_refuses_invalid_task_metadata(stacked_repo, capsys, fields, message):
    _, ledger, repo, _ = stacked_repo
    ledger.rows["t1"].update(fields)
    before = _git(repo, "rev-parse", "HEAD")
    capsys.readouterr()
    assert restack() == 1
    assert message in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD") == before
    assert ledger.updates == []


def test_restack_waits_for_every_blocker(stacked_repo, capsys):
    _, ledger, repo, _ = stacked_repo
    ledger.rows["a"]["state"] = "pr"
    capsys.readouterr()
    assert restack() == 1
    assert "unfinished parked dependency" in capsys.readouterr().err
    assert ledger.updates == []


@pytest.mark.parametrize("branch", ["dev", "main", "master", "v1"])
def test_restack_refuses_protected_branches(stacked_repo, capsys, branch):
    _, ledger, repo, _ = stacked_repo
    if branch == "dev":
        _git(repo, "switch", branch)
    else:
        _git(repo, "switch", "-c", branch)
    before = _git(repo, "rev-parse", "HEAD")
    capsys.readouterr()
    assert restack() == 1
    assert capsys.readouterr().err.startswith("swarm: restack needs a task worktree branch")
    assert _git(repo, "rev-parse", "HEAD") == before
    assert ledger.updates == []


@pytest.mark.parametrize("tracked", [True, False])
def test_restack_refuses_uncommitted_work(stacked_repo, capsys, tracked):
    _, ledger, repo, _ = stacked_repo
    file = repo / ("shared.txt" if tracked else "new.txt")
    file.write_text("keep my work\n")
    capsys.readouterr()
    assert restack() == 1
    assert capsys.readouterr().err == "swarm: restack needs a clean worktree\n"
    assert file.read_text() == "keep my work\n"
    assert ledger.updates == []


def test_restack_refuses_detached_head(stacked_repo, capsys):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "switch", "--detach")
    capsys.readouterr()
    assert restack() == 1
    assert capsys.readouterr().err.startswith("swarm: restack needs a task worktree branch")
    assert ledger.updates == []


def test_restack_refuses_a_base_outside_task_history(stacked_repo, capsys):
    _, ledger, repo, _ = stacked_repo
    ledger.rows["t1"]["stacked_base"] = _git(repo, "rev-parse", "origin/dev")
    capsys.readouterr()
    assert restack() == 1
    assert "stacked base is not in this branch" in capsys.readouterr().err
    assert ledger.updates == []


def test_restack_refuses_a_branch_without_parked_task_work(stacked_repo, capsys):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "fetch", "origin")
    _git(repo, "switch", "-c", "unrelated", "origin/dev")
    (repo / "other.txt").write_text("other work\n")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "Other work")
    before = _git(repo, "rev-parse", "HEAD")
    capsys.readouterr()
    assert restack() == 1
    assert "parked branch" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD") == before
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert ledger.updates == []


def test_restack_can_finish_after_the_engineer_resolves_a_conflict(stacked_repo, capsys):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "switch", "dev")
    (repo / "shared.txt").write_text("integration work\n")
    _git(repo, "commit", "-am", "Integration work")
    _git(repo, "push", "origin", "dev")
    _git(repo, "switch", "finisher")
    assert restack() == 1
    (repo / "shared.txt").write_text("resolved task work\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "-c", "core.editor=true", "rebase", "--continue")
    before = _git(repo, "rev-parse", "HEAD")
    assert restack() == 0
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "log", "--format=%s", "origin/dev..HEAD") == "Task work"
    assert ledger.rows["t1"]["parked_on"] == []


@pytest.mark.parametrize("keep_history", [True, False])
def test_restack_recovers_a_conflict_when_dev_keeps_the_stacked_base(stacked_repo, keep_history):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "switch", "dev")
    _git(repo, "merge", "--no-edit", "blocker")
    (repo / "shared.txt").write_text("integration work\n")
    _git(repo, "commit", "-am", "Integration work")
    _git(repo, "push", "origin", "dev")
    _git(repo, "switch", "finisher")
    assert restack() == 1
    if keep_history:
        (repo / "shared.txt").write_text("resolved task work\n")
        _git(repo, "add", "shared.txt")
        _git(repo, "-c", "core.editor=true", "rebase", "--continue")
    else:
        _git(repo, "rebase", "--skip")
    assert restack() == 0
    assert ledger.rows["t1"]["parked_on"] == []
    assert _git(repo, "merge-base", "origin/dev", "HEAD") == _git(repo, "rev-parse", "origin/dev")
    assert not (repo / _git(repo, "rev-parse", "--git-path", "agentihooks-restack.json")).exists()


def test_restack_fetch_failure_preserves_parked_work(stacked_repo, capsys):
    _, ledger, repo, _ = stacked_repo
    _git(repo, "remote", "set-url", "origin", str(repo / "missing"))
    capsys.readouterr()
    assert restack() == 1
    assert "cannot fetch origin" in capsys.readouterr().err
    assert ledger.updates == []


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (["git", "symbolic-ref"], "restack needs a task worktree branch"),
        (["git", "status"], "cannot inspect the worktree"),
        (["git", "fetch"], "cannot fetch origin"),
        (["git", "rev-parse", "--verify"], "cannot resolve the stacked base"),
        (["git", "rev-parse", "--git-path"], "cannot locate restack state"),
        (["git", "rev-parse", "origin/dev"], "cannot resolve origin/dev"),
    ],
)
def test_restack_reports_failed_git_commands(stacked_repo, monkeypatch, capsys, command, message):
    _, ledger, repo, _ = stacked_repo
    real_shell = stack.shell

    def failed(argv):
        if argv[: len(command)] == command:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="git failure\n")
        return real_shell(argv)

    monkeypatch.setattr(stack, "shell", failed)
    capsys.readouterr()
    before = _git(repo, "rev-parse", "HEAD")
    assert restack() == 1
    assert capsys.readouterr().err == f"swarm: {message}: git failure\n"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert ledger.updates == []


def test_restack_reports_failure_to_list_conflicts(stacked_repo, monkeypatch, capsys):
    _, ledger, _, _ = stacked_repo
    real_shell = stack.shell

    def failed(argv):
        if argv[1] in ("rebase", "diff"):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="git failure\n")
        return real_shell(argv)

    monkeypatch.setattr(stack, "shell", failed)
    capsys.readouterr()
    assert restack() == 1
    assert capsys.readouterr().err == "swarm: cannot list conflicted files: git failure\n"
    assert ledger.updates == []


@pytest.mark.parametrize("valid_context", [True, False])
def test_restack_refuses_a_checkpoint_without_both_identity_and_target_history(stacked_repo, capsys, valid_context):
    _, ledger, repo, base = stacked_repo
    _git(repo, "fetch", "origin")
    _git(repo, "switch", "-c", "unrelated", "origin/dev")
    path = repo / _git(repo, "rev-parse", "--git-path", "agentihooks-restack.json")
    context = ["sw", "t1", "unrelated", base] if valid_context else ["wrong"]
    onto = base if valid_context else _git(repo, "rev-parse", "origin/dev")
    path.write_text(json.dumps({"context": context, "onto": onto}))
    capsys.readouterr()
    before = _git(repo, "rev-parse", "HEAD")
    assert restack() == 1
    assert capsys.readouterr().err == (
        "swarm: the stacked base is not in this branch; use a worktree cut from the parked branch\n"
    )
    assert _git(repo, "rev-parse", "HEAD") == before
    assert ledger.rows["t1"]["parked_on"] == ["a"]
    assert ledger.updates == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"parked_on": []}, "this task has no parked work with a stacked base"),
        ({"stacked_base": ""}, "this task has no parked work with a stacked base"),
        ({"parked_on": ["missing"]}, "this task still has an unfinished parked dependency"),
    ],
)
def test_restack_metadata_refusals_name_the_missing_requirement(stacked_repo, capsys, fields, message):
    _, ledger, _, _ = stacked_repo
    ledger.rows["t1"].update(fields)
    capsys.readouterr()
    assert restack() == 1
    assert capsys.readouterr().err == f"swarm: {message}\n"
    assert ledger.updates == []
