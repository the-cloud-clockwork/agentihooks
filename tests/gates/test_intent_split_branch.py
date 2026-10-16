import subprocess

import pytest

from scripts.gates import intent
from scripts.gates.base import Call
from tests.gates.test_intent import NOW, TASK, WHO, bash, verdicts
from tests.gates.test_intent_split_gate import FIRST, SECOND, judged, split_task

pytestmark = pytest.mark.unit

VIEWED = {"capture_output": True, "text": True, "timeout": intent.GH_TIMEOUT_SEC}
UNRESOLVED = f"task {TASK} is split: name the pull request url in the merge so intent matches it to its judged half."


class Viewed:
    def __init__(self, url, code=0):
        self.url, self.code, self.calls = url, code, []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, self.code, stdout=f"{self.url}\n", stderr="")


def view(*operands):
    return ["gh", "pr", "view", *operands, "--json", "url", "--jq", ".url"]


@pytest.mark.parametrize(
    "command, args",
    [
        ("gh pr merge --squash", view()),
        ("gh pr merge half-two --squash -t done", view("half-two")),
        ("gh pr merge --subject done half-two", view("half-two")),
        ("gh pr merge -R o/r half-two", view("half-two", "-R", "o/r")),
        ("gh pr merge --repo o/r", view("--repo", "o/r")),
        ("gh pr merge half-two other", view("half-two")),
        ("FOO=1 && gh pr merge -t", view()),
        ("gh pr merge -R", view("-R", "")),
        ("gh pr merge half-two --repo=o/r", view("half-two", "--repo=o/r")),
        ("gh pr merge -Ro/r", view("-Ro/r")),
    ],
)
@pytest.mark.parametrize("url, allowed", [(FIRST, True), (SECOND, False)])
def test_a_merge_by_branch_or_unnamed_is_matched_to_its_half(tmp_path, command, args, url, allowed):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)
    run = Viewed(url)
    call = Call("Bash", {"command": command}, cwd="/work/tree")
    assert intent.IntentGate(run=run).decide(call, WHO, verdicts(tmp_path)).allowed is allowed
    assert run.calls == [(args, {**VIEWED, "cwd": "/work/tree"})]


@pytest.mark.parametrize(
    "flag", ["-A", "--author-email", "-b", "--body", "-F", "--body-file", "--match-head-commit", "-t", "--subject"]
)
def test_a_merge_option_value_is_not_read_as_the_branch(tmp_path, flag):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)
    run = Viewed(FIRST)
    assert (
        intent.IntentGate(run=run).decide(bash(f"gh pr merge {flag} value half-two"), WHO, verdicts(tmp_path)).allowed
    )
    assert run.calls[0][0] == view("half-two")


def test_a_pull_view_without_a_working_folder_runs_in_the_hook_folder(tmp_path):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)
    run = Viewed(FIRST)
    assert intent.IntentGate(run=run).decide(bash("gh pr merge"), WHO, verdicts(tmp_path)).allowed
    assert run.calls == [(view(), {**VIEWED, "cwd": None})]


def test_a_resolved_merge_names_the_bare_pull_request_url():
    assert intent.IntentGate(run=Viewed(SECOND))._named(["gh", "pr", "merge"], "") == ["gh", "pr", "merge", SECOND]


@pytest.mark.parametrize("url, code", [("", 1), (FIRST, 1), ("not a pull request", 0)])
def test_a_split_merge_that_names_no_resolvable_pull_is_refused(tmp_path, url, code):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)
    decision = intent.IntentGate(run=Viewed(url, code)).decide(bash("gh pr merge half-two"), WHO, verdicts(tmp_path))
    assert (decision.allowed, decision.reason) == (False, UNRESOLVED)


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired(["gh"], 20), OSError("no gh")])
def test_a_failed_pull_view_refuses_the_split_merge(tmp_path, error):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)

    def broken(args, **kwargs):
        raise error

    decision = intent.IntentGate(run=broken).decide(bash("gh pr merge"), WHO, verdicts(tmp_path))
    assert (decision.allowed, decision.reason) == (False, UNRESOLVED)


def test_a_branch_merge_of_a_registered_half_reads_that_halfs_judgment(tmp_path, monkeypatch):
    judged(tmp_path, split_task(split_pulls=[FIRST, SECOND]))
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    second = intent.IntentGate(run=Viewed(SECOND)).decide(bash("gh pr merge half-two"), WHO, verdicts(tmp_path))
    assert second.reason == "Intent must be checked on this pull request head and contract before it can merge."
    assert intent.IntentGate(run=Viewed(FIRST)).decide(bash("gh pr merge"), WHO, verdicts(tmp_path)).allowed


def test_a_registered_split_merge_that_names_no_resolvable_pull_is_refused(tmp_path):
    judged(tmp_path, split_task(split_pulls=[FIRST, SECOND]))
    decision = intent.IntentGate(run=Viewed("", 1)).decide(bash("gh pr merge"), WHO, verdicts(tmp_path))
    assert (decision.allowed, decision.reason) == (False, UNRESOLVED)


def test_an_unsplit_task_merges_by_branch_without_reading_github(tmp_path):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW)
    run = Viewed(SECOND)
    assert intent.IntentGate(run=run).decide(bash("gh pr merge half-two"), WHO, verdicts(tmp_path)).allowed
    assert run.calls == []


@pytest.mark.parametrize("command", [f"gh pr merge {FIRST}", "gh pr merge 9", "agentihooks swarm demo done"])
def test_a_named_split_merge_or_release_reads_no_pull_view(tmp_path, command):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)
    run = Viewed(SECOND)
    assert intent.IntentGate(run=run).decide(bash(command), WHO, verdicts(tmp_path)).allowed
    assert run.calls == []
