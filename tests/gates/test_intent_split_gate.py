import subprocess

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
    assert intent._merge_target([]) is None
    assert intent._merge_target(["FLAG=value"]) is None
    assert intent._merge_target(["gh", "pr", "merge", "--squash", "12"]) == "12"
    assert intent._merge_target(["gh", "pr", "merge", FIRST]) == FIRST
    assert intent._merge_target(["gh", "pr", "merge"]) is None
    assert intent._merge_target(["agentihooks", "swarm", SLUG, "merge", "queue", SECOND]) == SECOND
    assert intent._merge_target(["agentihooks", "swarm", SLUG, "done", "--pr", SECOND]) is None
    assert intent._merge_target(["git", "merge", "dev"]) is None
    assert intent._merge_target(["gh", "pr", "view", "12"]) is None
    assert intent._merge_target(["agentihooks", "swarm", SLUG, "merge", "state", SECOND]) is None


@pytest.mark.parametrize("target", [FIRST, "9"])
@pytest.mark.parametrize("head,allowed", [("abc", True), ("new", True), (None, True)])
def test_the_tests_first_intent_survives_recording_the_main_pull(tmp_path, monkeypatch, target, head, allowed):
    task = split_task()
    judged(tmp_path, task)
    monkeypatch.setattr(intent, "stamp_body", lambda *args: True)
    intent.stamp(SLUG, TASK, SECOND, {**DOC, "tasks": [task]}, "enforce", NOW + 1, home=tmp_path)
    monkeypatch.setattr(intent, "pr_head", lambda url: head if url == FIRST else "main")
    state = verdicts(tmp_path)
    decision = intent.IntentGate().decide(bash(f"agentihooks swarm {SLUG} merge queue {target}"), WHO, state)
    assert decision.allowed is allowed
    assert state.read(TASK)["verdict"] == "pending"


def test_the_main_intent_does_not_override_a_failed_tests_first_pull(tmp_path, monkeypatch):
    task = split_task()
    judged(tmp_path, task, verdict="fail")
    main = {**task, "pr_url": SECOND, "split_pulls": [FIRST, SECOND]}
    judged(tmp_path, main)
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    decision = intent.IntentGate().decide(bash(f"gh pr merge {FIRST}"), WHO, verdicts(tmp_path))
    assert not decision.allowed
    assert "failed" in decision.reason


@pytest.mark.parametrize("mode, allowed", [("enforce", False), ("coach", True), ("observe", True), ("off", True)])
def test_queue_uses_the_registered_pulls_current_judgment(tmp_path, monkeypatch, mode, allowed):
    task = split_task()
    judged(tmp_path, task, verdict="fail")
    judged(tmp_path, {**task, "pr_url": SECOND, "split_pulls": [FIRST, SECOND]})
    reads = []

    def head(url):
        assert url == FIRST
        reads.append(url)
        return "abc"

    monkeypatch.setattr(intent, "pr_head", head)
    decision = intent.IntentGate().decide(
        bash(f"agentihooks swarm {SLUG} merge queue {FIRST}"), WHO, verdicts(tmp_path), mode
    )
    assert decision.allowed is allowed
    assert reads == ([] if mode == "off" else [FIRST])


def test_queue_refuses_an_unregistered_split_pull_with_its_reason(tmp_path):
    judged(tmp_path, split_task())
    decision = intent.IntentGate().decide(
        bash(f"agentihooks swarm {SLUG} merge queue https://github.com/o/r/pull/99"), WHO, verdicts(tmp_path)
    )
    assert not decision.allowed
    assert decision.reason == "Intent requires a pull request registered on this task."


@pytest.mark.parametrize("missing", [False, True])
def test_queue_does_not_wait_for_an_unjudged_split_contract(tmp_path, monkeypatch, missing):
    from scripts.gates.verdicts import Verdicts

    judged(tmp_path, split_task(), verdict="fail")
    pulls = Verdicts(SLUG, "intent-pulls", tmp_path)
    if missing:
        pulls.clear(intent._pull_subject(TASK, FIRST))
    else:
        bound = pulls.read(TASK)
        pulls.write(TASK, "pass", "registered split pulls", NOW, pulls=bound["pulls"], inputs={FIRST: "new contract"})
    monkeypatch.setattr(intent, "pr_head", lambda url: pytest.fail("there is no current judgment"))
    decision = intent.IntentGate().decide(
        bash(f"agentihooks swarm {SLUG} merge queue {FIRST}"), WHO, verdicts(tmp_path)
    )
    assert decision.allowed


def test_listed_intent_refuses_a_pull_without_its_own_judgment(tmp_path, monkeypatch):
    task = split_task(split_pulls=[FIRST, SECOND])
    judged(tmp_path, task)
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    for url in [SECOND, "https://github.com/o/r/pull/99"]:
        assert not intent.IntentGate().decide(bash(f"gh pr merge {url}"), WHO, verdicts(tmp_path)).allowed


def test_recording_a_new_half_marks_it_registered_before_its_tick(tmp_path, monkeypatch):
    task = split_task()
    judged(tmp_path, task)
    monkeypatch.setattr(intent, "stamp_body", lambda *args: True)
    intent.stamp(SLUG, TASK, SECOND, {**DOC, "tasks": [task]}, "enforce", NOW + 1, home=tmp_path)
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    decision = intent.IntentGate().decide(bash(f"gh pr merge {SECOND}"), WHO, verdicts(tmp_path))
    assert not decision.allowed
    assert decision.reason == "Intent must be checked on this pull request head and contract before it can merge."
    foreign = intent.IntentGate().decide(bash("gh pr merge 99"), WHO, verdicts(tmp_path))
    assert not foreign.allowed
    assert foreign.reason == "Intent requires a pull request registered on this task."


def test_an_earlier_half_keeps_the_coach_shortfall_after_two_fix_rounds(tmp_path, monkeypatch):
    task = split_task()
    for index, head in enumerate(["first", "fixed once", "fixed twice"]):
        intent.Check(
            SLUG,
            "coach",
            NOW + index,
            Ledger(),
            Mail(),
            lambda url: {**VIEW, "head": head},
            lambda state: ("fail", "missing behavior"),
            home=tmp_path,
        ).run({**DOC, "tasks": [task]})
    judged(tmp_path, {**task, "pr_url": SECOND, "split_pulls": [FIRST, SECOND]})
    monkeypatch.setattr(intent, "pr_head", lambda url: "fixed twice" if url == FIRST else "abc")
    monkeypatch.setattr(intent, "pr_merged", lambda url: False)
    decision = intent.IntentGate().decide(bash(f"gh pr merge {FIRST}"), WHO, verdicts(tmp_path), "coach")
    assert decision.allowed


@pytest.mark.parametrize(
    "suffix", ["gh pr merge", f"agentihooks swarm {SLUG} ready handoff.md", f"agentihooks swarm {SLUG} done"]
)
@pytest.mark.parametrize("prefix", [f"gh pr merge {FIRST}", f"agentihooks swarm {SLUG} merge queue {FIRST}"])
def test_a_passing_pull_does_not_override_a_failed_command_in_the_same_call(tmp_path, monkeypatch, suffix, prefix):
    task = split_task()
    judged(tmp_path, task)
    main = {**task, "pr_url": SECOND, "split_pulls": [FIRST, SECOND]}
    judged(tmp_path, main, verdict="fail")
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    gate = intent.IntentGate(run=lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=SECOND))
    decision = gate.decide(bash(f"{prefix}; {suffix}"), WHO, verdicts(tmp_path))
    assert not decision.allowed
    assert "failed" in decision.reason


def test_a_ready_earlier_pull_cannot_clear_the_main_pulls_draft_hold(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    task = split_task()
    judged(tmp_path, task)
    main = {**task, "pr_url": SECOND, "split_pulls": [FIRST, SECOND]}
    intent.Check(
        SLUG,
        "enforce",
        NOW,
        Ledger(),
        Mail(),
        lambda url: {**VIEW, "draft": True},
        lambda state: ("pass", "unused"),
        home=tmp_path,
    ).run({**DOC, "tasks": [main]})
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    looked_up = []

    def ready(command, **kwargs):
        looked_up.append(command)
        return CompletedProcess(command, 0, "false\n", "")

    gate = intent.IntentGate(clock=lambda: (NOW + intent.GRACE_MS + 1) / 1000, run=ready)
    state = verdicts(tmp_path)
    decision = gate.decide(bash(f"gh pr merge {FIRST}; agentihooks swarm {SLUG} ready handoff.md"), WHO, state)
    assert not decision.allowed
    assert state.read(TASK)["reason"] == intent.DRAFT
    assert looked_up == []


def test_a_reused_legacy_coach_verdict_can_queue_its_registered_pull(tmp_path, monkeypatch):
    from scripts.gates.verdicts import Verdicts

    task = split_task()
    Verdicts(SLUG, "intent-coach", tmp_path).write(
        TASK,
        "pass",
        "judged",
        NOW,
        head="abc",
        url=FIRST,
        phase="p8",
        coach_rounds=0,
        inputs=intent._fingerprint(DOC, task),
    )
    monkeypatch.setattr(intent, "stamp_body", lambda *args: True)
    intent.stamp(SLUG, TASK, FIRST, {**DOC, "tasks": [task]}, "coach", NOW + 1, home=tmp_path)
    seen = []
    intent.Check(
        SLUG,
        "coach",
        NOW + 2,
        Ledger(),
        Mail(),
        lambda url: seen.append(url),
        lambda state: ("fail", "unused"),
        home=tmp_path,
        head=lambda url: "abc",
    ).run({**DOC, "tasks": [task]})
    assert seen == []
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    assert intent.IntentGate().decide(bash(f"gh pr merge {FIRST}"), WHO, verdicts(tmp_path), "coach").allowed


@pytest.mark.parametrize("changed", ["task", "phase"])
def test_a_changed_contract_invalidates_the_earlier_pull_judgment(tmp_path, monkeypatch, changed):
    task = split_task()
    judged(tmp_path, task)
    main = {**task, "pr_url": SECOND, "split_pulls": [FIRST, SECOND]}
    doc = DOC
    if changed == "task":
        main = {**main, "description": "Changed task contract."}
    else:
        doc = {**DOC, "phases": [{"id": "p8", "title": "Gates", "description": "Changed phase contract."}]}
    intent.Check(
        SLUG,
        "enforce",
        NOW + 1,
        Ledger(),
        Mail(),
        lambda url: VIEW,
        lambda state: ("fail", "changed contract"),
        home=tmp_path,
    ).run({**doc, "tasks": [main]})
    monkeypatch.setattr(intent, "pr_head", lambda url: "abc")
    assert not intent.IntentGate().decide(bash(f"gh pr merge {FIRST}"), WHO, verdicts(tmp_path)).allowed
