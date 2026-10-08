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
ROOT = "/home/me/dev/worktrees"
TOP = f"{ROOT}/repo/engineer-a1b2c3-0001"
LOCATE = [
    ["git", "rev-parse", "--show-toplevel"],
    ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
    ["bash", str(stack.WT_SCRIPT), "root"],
]
REMOVE = ["bash", str(stack.WT_SCRIPT), "done", "engineer-a1b2c3-0001", "--repo", TOP]


class Shell:
    def __init__(self):
        self.calls, self.remote, self.issues, self.fail = [], HEAD, json.dumps({"hasIssuesEnabled": True}), ()
        self.status, self.top, self.origin = "", TOP, "git@github.com:O/r.git"

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


def test_park_refuses_a_worktree_with_uncommitted_changes(parked, capsys):
    parked[3].status = " M scripts/x.py"
    _refused(parked, capsys, "the worktree holds uncommitted changes; commit them and push eng-t1 first")
    assert parked[3].calls[-1] == ["git", "status", "--porcelain"]


@pytest.mark.parametrize(
    "top", ["/elsewhere/engineer-a1b2c3-0001", f"{ROOT}/other/engineer-a1b2c3-0001", "/home/me/dev/repo"]
)
def test_park_refuses_a_worktree_outside_the_worktree_script_root(parked, capsys, top):
    parked[3].top = top
    _refused(parked, capsys, f"park removes only a worktree wt.sh new made under {ROOT}; {top} is not one")
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
    assert ("t1", {"parked_on": ["a"], "stacked_base": BASE}, AGENT) in ledger.updates
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
        "worktree_removed": TOP,
        "next": "stop now; the task waits on its branch",
    }


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
        ["git", "fetch", OTHER, "eng-a"],
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
    assert ("t1", {"parked_on": ["a"], "stacked_base": BASE}, AGENT) in ledger.updates
    assert "parked_repos" not in ledger.rows["t1"]
    assert ["git", "remote", "get-url", "origin"] in shell.calls
    assert ["git", "fetch", "origin", "eng-a"] in shell.calls
    assert ["git", "merge-base", "HEAD", "origin/eng-a"] in shell.calls
    [comment] = shell.comments()
    assert "a (`eng-a`) merges" in comment[-1]


def test_park_names_each_other_repository_once_in_dependency_order(parked):
    _, ledger, _, shell, doc = parked
    ledger.rows["b"].update({"state": "claimed", "branch_repo": OTHER})
    ledger.rows["c"] = {"id": "c", "title": "Docs", "state": "pr", "branch": "eng-c", "branch_repo": "/srv/docs.git"}
    ledger.rows["a"]["branch_repo"] = OTHER
    ledger.rows["t1"]["depends_on"] = ["a", "b", "c"]
    assert park(doc) == 0
    assert ledger.rows["t1"]["parked_repos"] == [OTHER, "/srv/docs.git"]
    assert shell.calls.count(["git", "remote", "get-url", "origin"]) == 1
    assert [c[2:] for c in shell.calls if c[:2] == ["git", "fetch"] and c[2] != "origin"] == [
        [OTHER, "eng-a"],
        [OTHER, "eng-b"],
        ["/srv/docs.git", "eng-c"],
    ]


@pytest.mark.parametrize(
    ("failing", "says"),
    [
        (("git", "remote", "get-url"), "cannot read the origin url"),
        (("git", "fetch", OTHER), f"cannot fetch eng-a from {OTHER}"),
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
    ],
)
def test_a_repository_key_ignores_scheme_user_case_and_suffix(url, key):
    assert stack.repo_key(url) == key


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://user:token@github.com/o/r.git", "https://github.com/o/r.git"),
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
    assert ("t1", {"parked_on": []}, AGENT) in ledger.updates
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"task": "t1", "parked_on": []}
    assert store.agents("sw")[0] == agent
    assert not (repo / _git(repo, "rev-parse", "--git-path", "agentihooks-restack.json")).exists()


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
