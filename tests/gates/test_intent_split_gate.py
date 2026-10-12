import pytest

from scripts.gates import intent
from tests.gates.test_intent import NOW, SLUG, TASK, WHO, Ledger, Mail, bash, verdicts

pytestmark = pytest.mark.unit

FIRST = "https://github.com/o/r/pull/9"
SECOND = "https://github.com/o/r/pull/10"
DOC = {"overview": "Gates in code.", "phases": [{"id": "p8", "title": "Gates", "description": "Stop failures."}]}
VIEW = {"title": "First half", "body": "", "files": ["scripts/gates/intent.py"], "head": "abc"}


def split_task(**fields):
    return {
        "id": TASK,
        "phase": "p8",
        "state": "pr",
        "title": "Split",
        "description": "Two halves.",
        "pr_url": FIRST,
        "claimed_by": WHO.name,
        "split": ["1-2", "3-4"],
        "split_pulls": [FIRST],
        **fields,
    }


def judged(tmp_path, task, verdict="pass"):
    intent.Check(
        SLUG, "enforce", NOW, Ledger(), Mail(), lambda url: VIEW, lambda state: (verdict, "judged"), home=tmp_path
    ).run({**DOC, "tasks": [task]})
    return verdicts(tmp_path).read(TASK)


def test_a_split_verdict_names_the_half_it_judged(tmp_path):
    record = judged(tmp_path, split_task())
    assert (record["verdict"], record["url"], record["split"]) == ("pass", FIRST, True)


def test_an_unsplit_verdict_carries_no_split_mark(tmp_path):
    record = judged(tmp_path, split_task(split=[], split_pulls=[]))
    assert "split" not in record and "url" not in record


def test_swarm_pr_on_a_split_task_marks_the_pending_verdict(tmp_path):
    doc = {**DOC, "tasks": [split_task(pr_url=SECOND)]}
    assert intent.stamp(SLUG, TASK, SECOND, doc, "enforce", NOW, home=tmp_path)["verdict"] == "pending"
    record = verdicts(tmp_path).read(TASK)
    assert (record["url"], record["split"]) == (SECOND, True)


@pytest.mark.parametrize("view", [lambda url: None, lambda url: {**VIEW, "draft": True}])
def test_a_split_verdict_held_pending_by_the_tick_keeps_its_half(tmp_path, view):
    task = split_task(pr_url=SECOND, split_pulls=[FIRST, SECOND])
    intent.Check(SLUG, "enforce", NOW, Ledger(), Mail(), view, lambda state: ("pass", "judged"), home=tmp_path).run(
        {**DOC, "tasks": [task]}
    )
    record = verdicts(tmp_path).read(TASK)
    assert (record["verdict"], record["url"], record["split"]) == ("pending", SECOND, True)
    assert not intent.IntentGate().decide(bash(f"gh pr merge {FIRST}"), WHO, verdicts(tmp_path)).allowed


@pytest.mark.parametrize(
    "command",
    [
        f"gh pr merge {SECOND} --squash",
        "gh pr merge 10",
        f"agentihooks swarm {SLUG} merge queue {SECOND}",
    ],
)
def test_a_half_the_verdict_did_not_judge_is_refused(tmp_path, command):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)
    decision = intent.IntentGate().decide(bash(command), WHO, verdicts(tmp_path))
    assert not decision.allowed
    assert decision.reason == (
        f"intent for task {TASK} was judged on {FIRST}, not this pull request. Record this half first: "
        f"agentihooks swarm {SLUG} pr <pull request url> --half <n>, then wait for its verdict."
    )


@pytest.mark.parametrize(
    "command",
    [
        f"gh pr merge {FIRST} --squash",
        "gh pr merge 9",
        "gh pr merge --squash",
        f"agentihooks swarm {SLUG} merge queue {FIRST}",
        f"agentihooks swarm {SLUG} done --pr {SECOND}",
    ],
)
def test_the_judged_half_or_no_named_target_passes(tmp_path, command):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, split=True)
    assert intent.IntentGate().decide(bash(command), WHO, verdicts(tmp_path)).allowed


def test_an_unsplit_task_may_merge_another_pull_request(tmp_path):
    verdicts(tmp_path).write(TASK, "pass", "judged", NOW, url=FIRST, coach_rounds=0, head="abc")
    assert intent.IntentGate().decide(bash(f"gh pr merge {SECOND}"), WHO, verdicts(tmp_path)).allowed


def test_merge_target_reads_the_first_pull_request_operand():
    assert intent._merge_target(["gh", "pr", "merge", "--squash", "12"]) == "12"
    assert intent._merge_target(["gh", "pr", "merge", FIRST]) == FIRST
    assert intent._merge_target(["gh", "pr", "merge"]) is None
    assert intent._merge_target(["agentihooks", "swarm", SLUG, "merge", "queue", SECOND]) == SECOND
    assert intent._merge_target(["agentihooks", "swarm", SLUG, "done", "--pr", SECOND]) is None
    assert intent._merge_target(["git", "merge", "dev"]) is None
    assert intent._merge_target(["gh", "pr", "view", "12"]) is None
    assert intent._merge_target(["agentihooks", "swarm", SLUG, "merge", "state", SECOND]) is None
