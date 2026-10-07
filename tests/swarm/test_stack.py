import json
import subprocess

import pytest

from scripts.swarm import stack, tick
from tests.swarm.test_cli import _handoff_doc, env, run

pytestmark = pytest.mark.xdist_group("fakeredis")
_fixture = env
AGENT = "engineer@a1b2c3-0001"
HEAD = "a" * 40
BASE = "b" * 40
ISSUE = "https://github.com/o/r/issues/7"


class Shell:
    def __init__(self, remote=HEAD, issues=True, fail=()):
        self.calls, self.remote, self.issues, self.fail = [], remote, issues, fail

    def __call__(self, argv):
        self.calls.append(argv)
        code = 1 if argv[:3] in [list(f) for f in self.fail] else 0
        out = {
            ("git", "rev-parse"): HEAD,
            ("git", "ls-remote"): f"{self.remote}\trefs/heads/{argv[-1]}" if self.remote else "",
            ("git", "merge-base"): BASE,
            ("git", "rev-list"): "3",
            ("gh", "repo"): json.dumps({"hasIssuesEnabled": self.issues}),
        }.get(tuple(argv[:2]), "")
        return subprocess.CompletedProcess(argv, code, stdout=out + "\n", stderr="boom" if code else "")

    def comments(self):
        return [c for c in self.calls if c[:3] == ["gh", "issue", "comment"]]


@pytest.fixture
def parked(env, monkeypatch, tmp_path):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.rows["a"] = {"id": "a", "title": "Build the parser", "state": "pr", "branch": "eng-a", "lane": "eng"}
    ledger.rows["b"] = {"id": "b", "title": "Write the schema", "state": "done", "branch": "eng-b", "lane": "eng"}
    ledger.rows["t1"].update({"depends_on": ["a", "b"], "branch": "eng-t1", "issue_url": ISSUE})
    shell = Shell()
    monkeypatch.setattr(stack, "shell", shell)
    return store, ledger, rt, shell, _handoff_doc(tmp_path)


def _untouched(store, ledger, shell, capsys, says):
    assert says in capsys.readouterr().err
    row = ledger.rows["t1"]
    assert "parked_on" not in row and "stacked_base" not in row
    assert ledger.comments == [] and shell.comments() == []
    assert store.handoff("sw", "t1") == ""
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["working"]


def test_park_refuses_a_task_without_a_recorded_branch(parked, capsys):
    store, ledger, _, shell, doc = parked
    ledger.rows["t1"]["branch"] = ""
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "records no branch")


def test_park_refuses_a_branch_missing_from_origin(parked, capsys):
    store, ledger, _, shell, doc = parked
    shell.remote = ""
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "branch eng-t1 is not on origin")


def test_park_refuses_a_branch_with_unpushed_commits(parked, capsys):
    store, ledger, _, shell, doc = parked
    shell.remote = "c" * 40
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "push eng-t1 first")


def test_park_refuses_a_task_without_an_issue_where_the_repo_has_issues(parked, capsys):
    store, ledger, _, shell, doc = parked
    ledger.rows["t1"]["issue_url"] = ""
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "the repo has issues")


def test_park_refuses_when_the_repo_answer_on_issues_is_unknown(parked, capsys):
    store, ledger, _, shell, doc = parked
    ledger.rows["t1"]["issue_url"] = ""
    shell.fail = [("gh", "repo", "view")]
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "cannot tell whether the repo has issues")


def test_park_refuses_a_task_with_no_open_dependency(parked, capsys):
    store, ledger, _, shell, doc = parked
    ledger.rows["a"]["state"] = "done"
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "no open dependency")


def test_park_refuses_when_no_open_dependency_records_a_branch(parked, capsys):
    store, ledger, _, shell, doc = parked
    ledger.rows["a"]["branch"] = ""
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "no open dependency records a branch")


def test_park_refuses_a_malformed_document(parked, capsys):
    store, ledger, _, shell, doc = parked
    doc.write_text(doc.read_text().replace("<!-- handoff complete -->", ""))
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    _untouched(store, ledger, shell, capsys, "handoff refused")


def test_park_refuses_when_the_issue_comment_fails(parked, capsys):
    store, ledger, _, shell, doc = parked
    shell.fail = [("gh", "issue", "comment")]
    assert run("sw", "--as", AGENT, "park", str(doc)) == 1
    assert "could not comment on the issue" in capsys.readouterr().err
    assert "parked_on" not in ledger.rows["t1"] and ledger.comments == []
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["working"]


def test_park_writes_the_open_dependencies_and_the_stacked_base(parked, capsys):
    store, ledger, _, shell, doc = parked
    assert run("sw", "--as", AGENT, "park", str(doc)) == 0
    row = ledger.rows["t1"]
    assert row["parked_on"] == ["a"] and row["stacked_base"] == BASE
    assert ["git", "fetch", "origin", "eng-a"] in shell.calls
    assert ["git", "merge-base", "HEAD", "origin/eng-a"] in shell.calls
    out = json.loads(capsys.readouterr().out)
    assert out["parked_on"] == ["a"] and out["stacked_base"] == BASE and "stop now" in out["next"]


def test_park_stacks_on_the_latest_of_several_dependency_branches(parked, monkeypatch):
    store, ledger, _, shell, doc = parked
    ledger.rows["b"]["state"] = "claimed"
    bases = {"origin/eng-a": "1" * 40, "origin/eng-b": "2" * 40}
    counts = {"1" * 40: "4", "2" * 40: "9"}

    def answer(argv):
        if argv[:2] == ["git", "merge-base"]:
            return subprocess.CompletedProcess(argv, 0, stdout=bases[argv[-1]] + "\n", stderr="")
        if argv[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=counts[argv[-1]] + "\n", stderr="")
        return shell(argv)

    monkeypatch.setattr(stack, "shell", answer)
    assert run("sw", "--as", AGENT, "park", str(doc)) == 0
    assert ledger.rows["t1"]["parked_on"] == ["a", "b"]
    assert ledger.rows["t1"]["stacked_base"] == "2" * 40


def test_park_comments_the_issue_naming_the_branch(parked):
    _, _, _, shell, doc = parked
    assert run("sw", "--as", AGENT, "park", str(doc)) == 0
    [comment] = shell.comments()
    assert comment[3] == ISSUE
    body = comment[comment.index("--body") + 1]
    assert "`eng-t1`" in body and "a (`eng-a`)" in body and BASE in body


def test_park_comments_the_ledger_task_naming_the_blocker_in_plain_words(parked):
    _, ledger, _, _, doc = parked
    assert run("sw", "--as", AGENT, "park", str(doc)) == 0
    assert ledger.comments == [
        (
            "t1",
            "Parked on its branch until Build the parser is done. "
            "The next engineer restacks it onto dev and finishes it.",
            AGENT,
        )
    ]


def test_park_without_issues_skips_the_issue_comment(parked):
    _, ledger, _, shell, doc = parked
    ledger.rows["t1"]["issue_url"] = ""
    shell.issues = False
    assert run("sw", "--as", AGENT, "park", str(doc)) == 0
    assert shell.comments() == [] and ledger.rows["t1"]["parked_on"] == ["a"]


def test_park_stores_the_handoff_and_retires_the_agent_so_the_reaped_task_waits(parked):
    store, ledger, rt, _, doc = parked
    assert run("sw", "--as", AGENT, "park", str(doc)) == 0
    assert store.handoff("sw", "t1") == doc.read_text()
    assert store.handoff_envelope("sw", "t1")["reason"] == "exit"
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["finished"]
    assert store.claimant("sw", "t1") == AGENT
    tick._reap("sw", store, ledger, rt, ledger.rows, 1)
    assert [a.name for a in store.agents("sw") if a.name == AGENT] == []
    assert ledger.rows["t1"]["state"] == "open" and ledger.rows["t1"]["parked_on"] == ["a"]
    assert store.handoff("sw", "t1") == doc.read_text()
