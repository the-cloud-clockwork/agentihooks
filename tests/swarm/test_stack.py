import json
import subprocess

import pytest

from scripts.handoff import check as handoff_check
from scripts.handoff import transfers
from scripts.inbox.seats import SeatRegistry
from scripts.swarm import cli, stack, tick
from scripts.swarm.store import SwarmError
from tests.swarm.test_cli import HANDOFF, env

pytestmark = pytest.mark.xdist_group("fakeredis")
_fixture = env
AGENT = "engineer@a1b2c3-0001"
HEAD = "a" * 40
BASE = "b" * 40
ISSUE = "https://github.com/o/r/issues/7"
NOW = 5_000
NOTE = "The next engineer restacks it onto dev and finishes it."
READ_FIRST = "- ledger:sw/tasks/t1 the task and its contract\n"


class Shell:
    def __init__(self):
        self.calls, self.remote, self.issues, self.fail = [], HEAD, json.dumps({"hasIssuesEnabled": True}), ()

    def __call__(self, argv):
        self.calls.append(argv)
        code = 1 if tuple(argv[:3]) in self.fail else 0
        out = {
            ("git", "rev-parse"): HEAD,
            ("git", "ls-remote"): f"{self.remote}\trefs/heads/{argv[-1]}" if self.remote else "",
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
    assert ledger.comments == [] and shell.comments() == []
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
        (("gh", "repo", "view"), "cannot tell whether the repo has issues"),
        (("git", "fetch", "origin"), "cannot fetch eng-a"),
        (("git", "merge-base", "HEAD"), "eng-a shares no history"),
        (("git", "rev-list", "--count"), f"cannot count {BASE}"),
        (("gh", "issue", "comment"), "could not comment on the issue"),
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


def test_park_refuses_a_malformed_document(parked, capsys):
    doc = parked[4]
    doc.write_text(doc.read_text().replace("<!-- handoff complete -->", ""))
    found = handoff_check.problems(doc.read_text(), lambda address: True)
    _refused(parked, capsys, handoff_check.refusal(found))


@pytest.mark.parametrize("address", ["recap:eng-1@sw", "workspace:t1/progress"])
def test_park_resolves_read_first_addresses_against_this_swarm(parked, capsys, address):
    doc = parked[4]
    doc.write_text(doc.read_text().replace(address, address.replace("1", "9")))
    assert park(doc) == 1
    assert "does not resolve" in capsys.readouterr().err


def test_park_writes_the_open_dependencies_and_the_stacked_base(parked, capsys):
    _, ledger, _, shell, doc = parked
    assert park(doc) == 0
    assert ("t1", {"parked_on": ["a"], "stacked_base": BASE}, AGENT) in ledger.updates
    assert ledger.rows["t1"]["parked_on"] == ["a"] and ledger.rows["t1"]["stacked_base"] == BASE
    body = f"Parked on branch `eng-t1` until a (`eng-a`) merges. Stacked base `{BASE}`. {NOTE}"
    assert shell.calls == [
        ["git", "ls-remote", "--heads", "origin", "eng-t1"],
        ["git", "rev-parse", "HEAD"],
        ["git", "fetch", "origin", "eng-a"],
        ["git", "merge-base", "HEAD", "origin/eng-a"],
        ["git", "rev-list", "--count", BASE],
        ["gh", "issue", "comment", ISSUE, "--body", body],
    ]
    assert json.loads(capsys.readouterr().out) == {
        "task": "t1",
        "parked_on": ["a"],
        "stacked_base": BASE,
        "next": "stop now; the task waits on its branch",
    }


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
    assert SeatRegistry(store.redis).exit_of(AGENT) == {"seat": "eng-1@sw", "reason": "handed off its seat"}
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["finished"]
    assert store.claimant("sw", "t1") == AGENT
    tick._reap("sw", store, ledger, rt, ledger.rows, 1)
    assert [a.name for a in store.agents("sw") if a.name == AGENT] == []
    assert ledger.rows["t1"]["state"] == "open" and ledger.rows["t1"]["parked_on"] == ["a"]
    assert store.handoff("sw", "t1") == text


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
