import json
import subprocess
from types import SimpleNamespace

import pytest

from hooks.classifier import ClassifierUnavailable, YesNo
from scripts.gates import Call, Gate, Who, entry, intent, intent_history
from scripts.gates.verdicts import Verdicts
from scripts.swarm import timing

SLUG, ME, TASK = "demo", "engineer@1-1", "t1"
WHO = Who(name=ME, swarm=SLUG, lane="eng", task=TASK)
URL = "https://github.com/o/r/pull/9"
NOW = 1_000_000_000
DOC = {
    "overview": "Gates in code.",
    "phases": [
        {"id": "p1", "title": "Other", "description": "not this"},
        {"id": "p8", "title": "Gates", "description": "Stop failures."},
    ],
    "tasks": [
        {
            "id": TASK,
            "phase": "p8",
            "title": "Intent check",
            "description": "Refuse merge on a failed check.",
            "state": "pr",
            "pr_url": URL,
            "claimed_by": ME,
        },
    ],
}
PR = {"title": "Add the intent gate", "body": "Closes 1", "files": ["scripts/gates/intent.py"]}


def bash(command):
    return Call("Bash", {"command": command})


def verdicts(tmp_path):
    return Verdicts(SLUG, "intent", tmp_path)


def rows(tmp_path):
    path = tmp_path / SLUG / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def answer(noul):
    return SimpleNamespace(noul=noul)


def classifier(usable, delivers=0.9, reachable=0.9, seen=None, weakens=0.1):
    def decide(state, questions, purpose):
        if seen is not None:
            seen.append((state, questions, purpose))
        return SimpleNamespace(
            answers={
                "usable": answer(usable),
                "delivers": answer(delivers),
                "reachable": answer(reachable),
                "weakens": answer(weakens),
            }
        )

    return decide


class Ran:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        code, out = result
        return subprocess.CompletedProcess(args, code, stdout=out, stderr="")


class TestGate:
    def test_it_is_a_registered_gate_that_ships_in_observe(self):
        gate = intent.IntentGate()
        assert isinstance(gate, Gate)
        assert (gate.name, gate.default_mode) == ("intent", "observe")
        assert isinstance(entry.GATES["intent"], intent.IntentGate)

    def test_it_matches_bash_calls_naming_merge_or_done(self):
        gate = intent.IntentGate()
        assert gate.matches(bash("gh pr merge 9 --squash"))
        assert gate.matches(bash("agentihooks swarm demo done --pr x"))
        assert not gate.matches(bash("gh pr view 9"))
        assert not gate.matches(Call("Read", {"command": "gh pr merge 9"}))

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr merge 9 --squash",
            "cd x && /usr/bin/gh pr merge --rebase 9",
            f"agentihooks swarm {SLUG} done --pr {URL}",
            f"env A=1 agentihooks swarm {SLUG} done",
            f"agentihooks swarm {SLUG} merge queue {URL}",
        ],
    )
    def test_a_failed_verdict_refuses_merge_and_done(self, tmp_path, command):
        verdicts(tmp_path).write(TASK, "fail", "the phase can use this change at probability 0.10", NOW)
        decision = intent.IntentGate().decide(bash(command), WHO, verdicts(tmp_path))
        assert not decision.allowed
        assert decision.reason == (
            f"intent check failed for task {TASK}: the phase can use this change at probability 0.10. "
            "Fix steps: Deliver the missing parent intent behavior identified in the verdict, "
            "add evidence that the phase can use it as delivered, commit and push the fix, "
            f"then run agentihooks swarm {SLUG} pr <url> for a new check."
        )

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr view 9",
            "gh pr checks 9",
            "echo gh pr merge",
            "agentihooks swarm demo pr x",
            "agentihooks swarm done",
            "agentihooks ledger demo done",
            f"agentihooks swarm {SLUG} merge state {URL}",
            f"agentihooks swarm {SLUG} merge dequeue {URL}",
            f"agentihooks swarm {SLUG} queue merge {URL}",
            "git merge dev",
            "A=1; gh pr view 9",
        ],
    )
    def test_other_commands_pass_a_failed_verdict(self, tmp_path, command):
        verdicts(tmp_path).write(TASK, "fail", "no", NOW)
        assert intent.IntentGate().decide(bash(command), WHO, verdicts(tmp_path)).allowed

    @pytest.mark.parametrize(
        "who",
        [Who(name=ME, swarm="", task=TASK), Who(name="", swarm=SLUG, task=TASK), Who(name=ME, swarm=SLUG, task="")],
    )
    def test_a_session_outside_a_swarm_task_passes(self, tmp_path, who):
        verdicts(tmp_path).write(TASK, "fail", "no", NOW)
        verdicts(tmp_path).write("", "fail", "no", NOW)
        assert intent.IntentGate().decide(bash("gh pr merge 9"), who, verdicts(tmp_path)).allowed

    @pytest.mark.parametrize("verdict", [None, "pass", "unchecked"])
    def test_no_verdict_a_pass_and_unchecked_let_merge_through(self, tmp_path, verdict):
        if verdict:
            verdicts(tmp_path).write(TASK, verdict, "fine", NOW)
        assert (
            intent.IntentGate(clock=lambda: NOW / 1000).decide(bash("gh pr merge 9"), WHO, verdicts(tmp_path)).allowed
        )
        assert rows(tmp_path) == []

    def test_the_refusal_names_the_whole_seconds_waited(self, tmp_path):
        verdicts(tmp_path).write(TASK, "pending", "intent check running", NOW)
        gate = intent.IntentGate(clock=lambda: (NOW + 100_000) / 1000)
        assert "started 100 s ago" in gate.decide(bash("gh pr merge 9"), WHO, verdicts(tmp_path)).reason

    def test_a_pending_verdict_refuses_for_two_minutes_then_passes_unchecked_and_counted(self, tmp_path):
        verdicts(tmp_path).write(TASK, "pending", "intent check running", NOW)
        early = intent.IntentGate(clock=lambda: (NOW + 119_999) / 1000)
        decision = early.decide(bash("gh pr merge 9"), WHO, verdicts(tmp_path))
        assert not decision.allowed
        assert decision.reason == (
            f"intent check running for task {TASK}, started 119 s ago; it passes unchecked at 120 s. Wait for it: "
            f'agentihooks swarm {SLUG} wait 2 --reason "intent check"'
        )
        assert rows(tmp_path) == []
        late = intent.IntentGate(clock=lambda: (NOW + 120_000) / 1000)
        assert late.decide(bash("gh pr merge 9"), WHO, verdicts(tmp_path)).allowed
        [row] = rows(tmp_path)
        assert (row["gate"], row["kind"], row["agent"], row["task"], row["tool"]) == (
            "intent",
            "count",
            ME,
            TASK,
            "Bash",
        )
        assert row["reason"] == "intent check still pending after two minutes, passed unchecked"


class TestBody:
    def test_the_section_names_project_phase_and_task(self):
        assert intent.section(DOC, DOC["tasks"][0]) == (
            f"{intent.START}\n## Parent intent\n\n**Project:** Gates in code.\n\n**Phase Gates:** Stop failures.\n\n"
            f"**Task {TASK}, Intent check:** Refuse merge on a failed check.\n{intent.END}"
        )

    def test_a_task_with_no_phase_gets_empty_phase_fields(self):
        text = intent.section({"overview": "o", "phases": []}, {"id": "x", "title": "T", "description": "d"})
        assert "**Phase :** \n" in text

    def test_with_intent_appends_once_and_replaces_after(self):
        first = intent.with_intent("Closes 1\n\n", "S1")
        assert first == "Closes 1\n\nS1"
        assert intent.with_intent("", "S1") == "S1"
        old = f"Top\n\n{intent.START}\nold\n{intent.END}\n\nTail"
        assert intent.with_intent(old, "NEW") == "Top\n\nNEW\n\nTail"

    def test_only_the_first_section_is_replaced_up_to_its_own_end(self):
        start, end = intent.START, intent.END
        assert intent.with_intent(f"{start}\na\n{end}\nmid {end}", "N\\1") == f"N\\1\nmid {end}"
        assert intent.with_intent(f"{start}a{end} mid {start}b{end}", "N") == f"N mid {start}b{end}"

    def test_a_start_marker_without_its_end_appends(self):
        assert intent.with_intent(f"x {intent.START} y", "S") == f"x {intent.START} y\n\nS"
        assert intent.with_intent(f"{intent.END} x {intent.START}", "S") == f"{intent.END} x {intent.START}\n\nS"

    def test_stamp_body_reads_the_body_and_patches_it_through_gh_api(self):
        ran = Ran((0, json.dumps({"body": "Closes 1"})), (0, "{}"))
        assert intent.stamp_body(URL, DOC, DOC["tasks"][0], run=ran)
        (read, read_kw), (patch, patch_kw) = ran.calls
        assert read == ["gh", "pr", "view", URL, "--json", "body"]
        assert patch == ["gh", "api", "--method", "PATCH", "repos/o/r/pulls/9", "--input", "-"]
        assert json.loads(patch_kw["input"]) == {"body": "Closes 1\n\n" + intent.section(DOC, DOC["tasks"][0])}
        for kwargs in (read_kw, patch_kw):
            assert (kwargs["capture_output"], kwargs["text"], kwargs["timeout"]) == (True, True, intent.GH_TIMEOUT_SEC)

    def test_a_null_body_is_treated_as_empty(self):
        ran = Ran((0, json.dumps({"body": None})), (0, "{}"))
        assert intent.stamp_body(URL, DOC, DOC["tasks"][0], run=ran)
        assert json.loads(ran.calls[1][1]["input"])["body"] == intent.section(DOC, DOC["tasks"][0])

    @pytest.mark.parametrize(
        "results",
        [
            [(1, "")],
            [(0, json.dumps({"body": "x"})), (1, "")],
            [(0, "not json")],
            [OSError("no gh")],
            [subprocess.TimeoutExpired("gh", 1)],
        ],
    )
    def test_stamp_body_reports_failure_without_raising(self, results):
        assert intent.stamp_body(URL, DOC, DOC["tasks"][0], run=Ran(*results)) is False

    def test_a_url_that_is_not_a_pull_request_is_not_stamped(self):
        ran = Ran()
        assert intent.stamp_body("https://example.com/x", DOC, DOC["tasks"][0], run=ran) is False
        assert ran.calls == []


@pytest.fixture
def stamped(monkeypatch):
    seen = []
    monkeypatch.setattr(intent, "stamp_body", lambda url, doc, task: seen.append((url, doc, task)) or bool(seen))
    return seen


class TestStamp:
    def test_stamp_arms_a_pending_verdict_and_writes_the_body(self, tmp_path, stamped):
        assert intent.stamp(SLUG, TASK, URL, DOC, "observe", NOW, home=tmp_path) == {
            "verdict": "pending",
            "body": True,
        }
        assert stamped == [(URL, DOC, DOC["tasks"][0])]
        assert verdicts(tmp_path).read(TASK) == {"verdict": "pending", "reason": "intent check running", "at": NOW}

    def test_off_writes_the_body_and_arms_nothing(self, tmp_path, stamped):
        assert intent.stamp(SLUG, TASK, URL, DOC, "off", NOW, home=tmp_path) == {"verdict": "off", "body": True}
        assert verdicts(tmp_path).read(TASK) is None

    def test_an_unknown_task_writes_no_body(self, tmp_path, stamped):
        assert intent.stamp(SLUG, "nope", URL, DOC, "enforce", NOW, home=tmp_path)["body"] is False
        assert stamped == []


class TestJudge:
    def test_judge_asks_four_yes_no_questions_under_its_purpose(self):
        seen = []
        assert intent.judge({"s": 1}, decide=classifier(0.8, seen=seen)) == (
            "pass",
            "the phase can use it as delivered at probability 0.80",
        )
        [(state, questions, purpose)] = seen
        assert (state, purpose) == ({"s": 1}, "intent-check")
        assert list(questions) == ["usable", "delivers", "reachable", "weakens"]
        assert all(isinstance(q, YesNo) for q in questions.values())

    def test_a_usable_change_that_weakens_the_phase_fails_with_that_reason(self):
        assert intent.judge({}, decide=classifier(0.96, weakens=0.5)) == (
            "fail",
            "the phase can use this change at probability 0.96; the change may weaken what the phase builds, at "
            "probability 0.50",
        )
        assert intent.judge({}, decide=classifier(0.96, weakens=0.49))[0] == "pass"
        assert intent.judge({}, decide=classifier(0.3, weakens=0.6)) == (
            "fail",
            "the phase can use this change at probability 0.30; the change may weaken what the phase builds, at "
            "probability 0.60",
        )

    def test_an_unusable_change_that_also_weakens_the_phase_names_both(self):
        assert intent.judge({}, decide=classifier(0.1, delivers=0.2, weakens=0.97)) == (
            "fail",
            "the phase can use this change at probability 0.10, under 0.3; the change may not deliver what the task "
            "text asks; the change may weaken what the phase builds, at probability 0.97",
        )

    def test_exactly_the_line_passes(self):
        assert intent.judge({}, decide=classifier(0.3))[0] == "pass"

    def test_under_the_line_fails_with_the_low_diagnostics_as_reasons(self):
        assert intent.judge({}, decide=classifier(0.29, delivers=0.49, reachable=0.2)) == (
            "fail",
            "the phase can use this change at probability 0.29, under 0.3; the change may not deliver what the task "
            "text asks; nothing in the change may let the phase reach it",
        )
        assert intent.judge({}, decide=classifier(0.1, delivers=0.5, reachable=0.5)) == (
            "fail",
            "the phase can use this change at probability 0.10, under 0.3",
        )

    def test_a_classifier_that_does_not_answer_gives_unchecked(self):
        def down(state, questions, purpose):
            raise ClassifierUnavailable("down")

        assert intent.judge({}, decide=down) == ("unchecked", "the classifier did not answer")


class TestState:
    def test_the_state_carries_intent_the_pull_request_and_the_proof(self, tmp_path):
        (tmp_path / "proof.md").write_text("x" * 10 + "tail of proof")
        task = {**DOC["tasks"][0], "workspace": str(tmp_path), "proof": {"command": "pytest"}}
        state = intent.state_of({**DOC, "tasks": [task]}, task, PR, proof_chars=13)
        assert state == {
            "overview": "Gates in code.",
            "phase": "Gates",
            "phase_intent": "Stop failures.",
            "task": "Intent check",
            "task_text": "Refuse merge on a failed check.",
            "pull_request_title": "Add the intent gate",
            "pull_request_body": "Closes 1",
            "changed_files": ["scripts/gates/intent.py"],
            "proof": {"command": "pytest"},
            "proof_notes": "tail of proof",
            "reviewer_findings": {},
        }

    def test_a_task_without_a_workspace_has_no_proof_notes(self):
        state = intent.state_of(DOC, DOC["tasks"][0], PR)
        assert (state["proof"], state["proof_notes"]) == ({}, "")
        missing = intent.state_of(DOC, {**DOC["tasks"][0], "workspace": "/nonexistent/x"}, PR)
        assert missing["proof_notes"] == ""

    def test_a_task_with_no_phase_has_empty_phase_fields(self):
        state = intent.state_of({**DOC, "phases": []}, DOC["tasks"][0], PR)
        assert (state["phase"], state["phase_intent"]) == ("", "")

    @pytest.mark.parametrize(
        "body,expected,draft",
        [
            (None, "", {}),
            ("Closes 4", "Closes 4", {"isDraft": False, "state": "OPEN"}),
            ("Closes 4", "Closes 4", {"isDraft": True}),
            ("Closes 4", "Closes 4", {"state": "MERGED"}),
        ],
    )
    def test_pr_view_reads_title_body_file_paths_draft_and_merged(self, body, expected, draft):
        raw = {"title": "T", "body": body, "files": [{"path": "a.py", "additions": 1}, {"path": "b.py"}], **draft}
        ran = Ran((0, "abc123\n"), (0, json.dumps(raw)), (0, ""), (0, "abc123\n"))
        assert intent.pr_view(URL, run=ran) == {
            "head": "abc123",
            "title": "T",
            "body": expected,
            "files": ["a.py", "b.py"],
            "draft": draft.get("isDraft", False),
            "merged": draft.get("state") == "MERGED",
            "reviewer_findings": {"reviews": [], "comments": [], "inline": []},
        }
        args, kwargs = ran.calls[1]
        assert args == ["gh", "pr", "view", URL, "--json", "title,body,files,reviews,comments,isDraft,state"]
        assert (kwargs["capture_output"], kwargs["text"], kwargs["timeout"]) == (True, True, intent.GH_TIMEOUT_SEC)

    @pytest.mark.parametrize("results", [[(1, "")], [(0, "nope")], [OSError("x")], [(0, json.dumps({"body": "b"}))]])
    def test_pr_view_returns_none_when_github_does_not_answer(self, results):
        ran = Ran((0, "head"), *results)
        assert intent.pr_view(URL, run=ran) is None


class Ledger:
    def __init__(self):
        self.updates, self.comments = [], []

    def update_task(self, slug, task_id, fields, by="swarm"):
        self.updates.append((slug, task_id, fields, by))

    def comment(self, slug, task_id, text, by):
        self.comments.append((slug, task_id, text, by))


class Mail:
    master = "master-seat"

    def __init__(self):
        self.sent = []

    def engineer(self, task):
        return f"seat-of-{task.get('claimed_by')}"

    def send(self, key, address, text, ref=""):
        self.sent.append((key, address, text, ref))
        return [f"told {address}: {key}"]


def check(tmp_path, mode="enforce", view=lambda url: PR, ask=None, ledger=None, mail=None):
    return intent.Check(SLUG, mode, NOW, ledger or Ledger(), mail or Mail(), view, ask, home=tmp_path)


def run_pass(tmp_path, mode="enforce", usable=0.1, doc=DOC, view=None):
    ledger, mail, viewed = Ledger(), Mail(), []

    def read(url):
        viewed.append(url)
        return PR if view is None else view

    def ask(state):
        return intent.judge(state, classifier(usable))

    actions = check(tmp_path, mode, read, ask, ledger, mail).run(doc)
    return SimpleNamespace(actions=actions, ledger=ledger, mail=mail, viewed=viewed)


FAIL_REASON = (
    "the phase can use this change at probability 0.10, under 0.3; What would meet intent: "
    "Deliver Intent check: Refuse merge on a failed check.. The phase must be able to use it for Gates: Stop failures.."
)
FAIL_TEXT = (
    f"The intent check failed: {FAIL_REASON}. Fix steps: Deliver the missing parent intent behavior identified in the verdict, "
    "add evidence that the phase can use it as delivered, commit and push the fix, "
    f"then run agentihooks swarm {SLUG} pr <url> for a new check."
)
FAIL_COMMENT = "The intent check failed. The engineer has the verdict and the fix steps in the inbox."


class TestCheckPass:
    def test_a_fail_under_enforce_returns_the_task_to_its_agent(self, tmp_path):
        verdicts(tmp_path).write(TASK, "pending", "intent check running", NOW - 5)
        got = run_pass(tmp_path)
        assert verdicts(tmp_path).read(TASK) == {
            "verdict": "fail",
            "reason": FAIL_REASON,
            "at": NOW,
            "phase": "p8",
            "inputs": intent._fingerprint(DOC, DOC["tasks"][0]),
        }
        assert got.viewed == [URL]
        assert got.ledger.updates == [(SLUG, TASK, {"state": "claimed"}, "swarm")]
        assert got.ledger.comments == [(SLUG, TASK, FAIL_COMMENT, "swarm")]
        assert got.mail.sent == [(f"intent-fail:{TASK}:{NOW}", f"seat-of-{ME}", FAIL_TEXT, f"tasks/{TASK}")]
        [row] = rows(tmp_path)
        assert (row["gate"], row["kind"], row["agent"], row["task"], row["reason"]) == (
            "intent",
            "deny",
            ME,
            TASK,
            FAIL_REASON,
        )
        assert got.actions == [f"task {TASK} intent check fail", f"told seat-of-{ME}: intent-fail:{TASK}:{NOW}"]

    def test_the_fail_comment_is_plain_words_the_ledger_accepts(self, tmp_path):
        from scripts.swarm_ledger.ledger_comments import problems

        verdicts(tmp_path).write(TASK, "pending", "intent check running", NOW - 5)
        [(_, _, text, _)] = run_pass(tmp_path).ledger.comments
        assert problems(text, "comment") == []
        assert problems(intent.SHORTFALL_COMMENT, "comment") == []

    def test_a_fail_under_observe_is_only_logged(self, tmp_path):
        got = run_pass(tmp_path, mode="observe")
        assert verdicts(tmp_path).read(TASK)["verdict"] == "fail"
        assert (got.ledger.updates, got.ledger.comments, got.mail.sent) == ([], [], [])
        assert [(r["kind"], r["agent"], r["reason"]) for r in rows(tmp_path)] == [("observe", ME, FAIL_REASON)]
        assert got.actions == [f"task {TASK} intent check fail"]

    def test_a_verdict_judged_under_another_phase_is_judged_again(self, tmp_path):
        run_pass(tmp_path)
        assert verdicts(tmp_path).read(TASK)["phase"] == "p8"
        moved = {**DOC, "tasks": [{**DOC["tasks"][0], "phase": "p1"}]}
        got = run_pass(tmp_path, usable=0.9, doc=moved)
        assert got.viewed == [URL]
        record = verdicts(tmp_path).read(TASK)
        assert (record["verdict"], record["phase"]) == ("pass", "p1")

    def test_a_verdict_under_the_same_phase_is_not_judged_again(self, tmp_path):
        run_pass(tmp_path)
        got = run_pass(tmp_path, usable=0.9)
        assert (got.viewed, verdicts(tmp_path).read(TASK)["verdict"]) == ([], "fail")

    def test_a_verdict_with_no_phase_is_judged_again(self, tmp_path):
        verdicts(tmp_path).write(TASK, "fail", "old", NOW - 5)
        got = run_pass(tmp_path, usable=0.9)
        assert got.viewed == [URL]
        assert verdicts(tmp_path).read(TASK)["verdict"] == "pass"

    def test_a_fail_from_another_phase_stops_denying_while_it_is_judged_again(self, tmp_path):
        verdicts(tmp_path).write(TASK, "fail", "old", NOW - 5, phase="p1")
        check(tmp_path, view=lambda url: None).run(DOC)
        assert verdicts(tmp_path).read(TASK)["verdict"] == "pending"
        intent.Check(SLUG, "enforce", NOW + 60_000, Ledger(), Mail(), lambda url: None, None, home=tmp_path).run(DOC)
        assert (verdicts(tmp_path).read(TASK)["verdict"], verdicts(tmp_path).read(TASK)["at"]) == ("pending", NOW)

    def test_off_skips_the_check(self, tmp_path):
        got = run_pass(tmp_path, mode="off")
        assert (got.actions, got.viewed, verdicts(tmp_path).read(TASK)) == ([], [], None)

    def test_a_task_in_pr_with_no_verdict_is_armed_and_judged_in_the_same_pass(self, tmp_path):
        got = run_pass(tmp_path, usable=0.9)
        assert verdicts(tmp_path).read(TASK) == {
            "verdict": "pass",
            "reason": "the phase can use it as delivered at probability 0.90",
            "at": NOW,
            "phase": "p8",
            "inputs": intent._fingerprint(DOC, DOC["tasks"][0]),
        }
        assert (got.ledger.updates, got.mail.sent, rows(tmp_path)) == ([], [], [])
        assert got.actions == [f"task {TASK} intent check pass"]

    @pytest.mark.parametrize("verdict", ["pass", "fail", "unchecked"])
    def test_a_judged_task_is_not_asked_again(self, tmp_path, verdict):
        inputs = intent._fingerprint(DOC, DOC["tasks"][0])
        verdicts(tmp_path).write(TASK, verdict, "done before", NOW - 5, phase="p8", inputs=inputs)
        got = run_pass(tmp_path)
        assert (got.actions, got.viewed) == ([], [])
        assert verdicts(tmp_path).read(TASK)["at"] == NOW - 5

    @pytest.mark.parametrize("task", [{"state": "claimed"}, {"pr_url": ""}, {"state": "done"}])
    def test_only_tasks_in_pr_with_a_pull_request_are_checked(self, tmp_path, task):
        doc = {**DOC, "tasks": [{**DOC["tasks"][0], **task}]}
        got = run_pass(tmp_path, doc=doc)
        assert (got.actions, got.viewed, verdicts(tmp_path).read(TASK)) == ([], [], None)

    def test_an_unreadable_pull_request_stays_pending(self, tmp_path):
        assert check(tmp_path, view=lambda url: None).run(DOC) == []
        assert verdicts(tmp_path).read(TASK) == {
            "verdict": "pending",
            "reason": "intent check running",
            "at": NOW,
            "phase": "p8",
        }

    def test_an_unanswered_classifier_writes_unchecked_and_counts_it(self, tmp_path):
        ledger, mail = Ledger(), Mail()
        down = check(tmp_path, ask=lambda s: ("unchecked", "the classifier did not answer"), ledger=ledger, mail=mail)
        assert down.run(DOC) == [f"task {TASK} intent check unchecked"]
        assert verdicts(tmp_path).read(TASK)["verdict"] == "unchecked"
        assert [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"]) for r in rows(tmp_path)] == [
            ("intent", "count", ME, TASK, "the classifier did not answer")
        ]
        assert (ledger.updates, mail.sent) == ([], [])

    def test_the_classifier_is_asked_with_the_task_state(self, tmp_path):
        seen = []
        check(tmp_path, "observe", ask=lambda s: seen.append(s) or ("pass", "ok")).run(DOC)
        assert seen == [intent.state_of(DOC, DOC["tasks"][0], PR)]

    def test_a_task_with_no_claim_is_logged_with_no_agent(self, tmp_path):
        task = {k: v for k, v in DOC["tasks"][0].items() if k != "claimed_by"}
        run_pass(tmp_path, mode="observe", doc={**DOC, "tasks": [task]})
        assert [(r["agent"], r["task"]) for r in rows(tmp_path)] == [("", TASK)]

    def test_skipped_tasks_do_not_stop_the_pass(self, tmp_path):
        base = DOC["tasks"][0]
        tasks = [
            {**base, "id": "a", "state": "claimed"},
            {**base, "id": "b"},
            {**base, "id": "c", "pr_url": "https://github.com/o/r/pull/404"},
            {**base, "id": "d"},
        ]
        verdicts(tmp_path).write("b", "pass", "judged", NOW - 5, phase="p8", inputs=intent._fingerprint(DOC, base))

        def view(url):
            return None if url.endswith("/404") else PR

        actions = check(tmp_path, "observe", view=view, ask=lambda s: ("pass", "ok")).run({**DOC, "tasks": tasks})
        assert actions == ["task d intent check pass"]
        assert [verdicts(tmp_path).read(t)["verdict"] for t in "bcd"] == ["pass", "pending", "pass"]


def counted_pass(tmp_path, mode, doc, asked, verdict="fail"):
    def ask(state):
        asked.append(state)
        return verdict, "judged"

    viewed = []
    check(tmp_path, mode, lambda url: viewed.append(url) or PR, ask).run(doc)
    return viewed


def changed(**fields):
    return {**DOC, "tasks": [{**DOC["tasks"][0], **fields}]}


@pytest.mark.parametrize("mode", ["enforce", "observe"])
class TestJudgedInputs:
    def test_a_verdict_carries_the_judged_inputs_fingerprint(self, tmp_path, mode):
        counted_pass(tmp_path, mode, DOC, [])
        assert verdicts(tmp_path).read(TASK)["inputs"] == intent._fingerprint(DOC, DOC["tasks"][0])

    @pytest.mark.parametrize(
        "change", [{"description": "Refuse merge and done on a failed check."}, {"title": "Intent gate"}]
    )
    def test_a_corrected_task_on_the_same_pull_request_is_judged_again(self, tmp_path, mode, change):
        asked = []
        counted_pass(tmp_path, mode, DOC, asked)
        counted_pass(tmp_path, mode, DOC, asked)
        assert len(asked) == 1
        corrected = changed(**change)
        assert counted_pass(tmp_path, mode, corrected, asked, "pass") == [URL]
        assert len(asked) == 2
        record = verdicts(tmp_path).read(TASK)
        assert (record["verdict"], record["inputs"]) == ("pass", intent._fingerprint(corrected, corrected["tasks"][0]))

    def test_a_changed_plan_slice_is_judged_again(self, tmp_path, mode, monkeypatch):
        asked, plan = [], {"text": "line one"}
        monkeypatch.setattr(intent.plan_read, "exact", lambda doc, task: plan["text"])
        sliced = changed(plan_lines="1-1")
        counted_pass(tmp_path, mode, sliced, asked)
        counted_pass(tmp_path, mode, sliced, asked)
        assert len(asked) == 1
        plan["text"] = "line one corrected"
        counted_pass(tmp_path, mode, sliced, asked)
        assert len(asked) == 2
        assert asked[-1]["plan_chunk"] == "line one corrected"

    def test_a_follow_up_mark_is_judged_again(self, tmp_path, mode, monkeypatch):
        asked = []
        monkeypatch.setattr(intent.plan_read, "exact", lambda doc, task: "line one")
        counted_pass(tmp_path, mode, changed(plan_lines="1-1"), asked)
        counted_pass(tmp_path, mode, changed(plan_lines="1-1", follow_up=True), asked)
        assert len(asked) == 2
        assert "plan_chunk" not in asked[-1]

    def test_an_unchanged_task_reuses_its_verdict_without_a_read_or_a_call(self, tmp_path, mode):
        asked = []
        counted_pass(tmp_path, mode, DOC, asked)
        before = verdicts(tmp_path).read(TASK)
        assert counted_pass(tmp_path, mode, DOC, asked, "pass") == []
        assert (len(asked), verdicts(tmp_path).read(TASK)) == (1, before)

    def test_a_record_without_a_fingerprint_is_judged_once(self, tmp_path, mode):
        verdicts(tmp_path).write(TASK, "pass", "judged before fingerprints", NOW - 5, phase="p8")
        asked = []
        counted_pass(tmp_path, mode, DOC, asked)
        counted_pass(tmp_path, mode, DOC, asked)
        assert len(asked) == 1
        assert verdicts(tmp_path).read(TASK)["inputs"] == intent._fingerprint(DOC, DOC["tasks"][0])

    def test_a_verdict_on_changed_inputs_stops_standing_while_it_is_judged_again(self, tmp_path, mode):
        counted_pass(tmp_path, mode, DOC, [], "pass")
        check(tmp_path, mode, view=lambda url: None).run(changed(title="Intent gate"))
        assert verdicts(tmp_path).read(TASK) == {
            "verdict": "pending",
            "reason": "intent check running",
            "at": NOW,
            "phase": "p8",
        }


class TestModeOf:
    @pytest.mark.parametrize(
        "value,expected",
        [("enforce", "enforce"), ("off", "off"), ("observe", "observe"), ("bogus", "observe"), (None, "observe")],
    )
    def test_the_swarm_setting_picks_the_tick_mode(self, value, expected):
        gates = {} if value is None else {"intent": value}
        assert intent.mode_of(SimpleNamespace(gates=gates)) == expected


PLAN = "".join(f"line {n}\n" for n in range(1, 41))
CHUNK_STATE = {"task_text": "", "plan_lines": "15-17", "plan_chunk": "line 15\n\nline 17\n"}
BASE_QUESTIONS = ["usable", "delivers", "reachable", "weakens"]
CHUNK_QUESTIONS = [*BASE_QUESTIONS, "underdelivers", "overdelivers", "misses_line_15", "misses_line_17"]


def chunk_classifier(underdelivers=0.1, overdelivers=0.1, misses=None, seen=None):
    def decide(state, questions, purpose):
        if seen is not None:
            seen.append(list(questions))
        found = {"usable": 0.9, "delivers": 0.9, "reachable": 0.9, "weakens": 0.1}
        found |= {"underdelivers": underdelivers, "overdelivers": overdelivers, **(misses or {})}
        return SimpleNamespace(answers={name: answer(found.get(name, 0.1)) for name in questions})

    return decide


@pytest.fixture
def planned(tmp_path, monkeypatch):
    from scripts.swarm_ledger import HERE

    monkeypatch.syspath_prepend(str(HERE))
    from scripts.swarm_ledger import ledger_artifacts

    monkeypatch.setattr(ledger_artifacts.core, "LEDGER_DIR", tmp_path)
    file = ledger_artifacts.store("chunks", "plan.md", PLAN.encode())
    ref = {"artifact": f"http://127.0.0.1:8765/artifacts/chunks/{file['id']}", "lines": "12-35"}
    phase = {**DOC["phases"][1], "plan_ref": ref}
    task = {**DOC["tasks"][0], "plan_lines": "15-17"}
    return {**DOC, "artifacts": [{"plan": True, "file": file}], "phases": [phase], "tasks": [task]}


class TestPlanChunk:
    def test_the_state_carries_the_exact_plan_chunk_without_margin(self, planned):
        state = intent.state_of(planned, planned["tasks"][0], PR)
        assert (state["plan_lines"], state["plan_chunk"]) == ("15-17", "line 15\nline 16\nline 17\n")

    def test_a_task_without_plan_lines_carries_no_plan_fields(self, planned):
        planned["tasks"][0].pop("plan_lines")
        assert intent.state_of(planned, planned["tasks"][0], PR) == intent.state_of(DOC, DOC["tasks"][0], PR)

    def test_a_follow_up_task_is_graded_by_its_description_alone(self, planned):
        planned["tasks"][0]["follow_up"] = True
        state = intent.state_of(planned, planned["tasks"][0], PR)
        assert state == intent.state_of(DOC, DOC["tasks"][0], PR)
        assert state["task_text"] == planned["tasks"][0]["description"]
        assert list(intent.questions_for(state)) == BASE_QUESTIONS

    @pytest.mark.parametrize(
        "change",
        [
            lambda doc: doc["phases"][0].pop("plan_ref"),
            lambda doc: doc.update(artifacts=[]),
            lambda doc: doc["tasks"][0].update(plan_lines="45-46"),
        ],
    )
    def test_an_unreadable_chunk_is_unchecked_without_asking_the_classifier(self, planned, change):
        change(planned)
        state = intent.state_of(planned, planned["tasks"][0], PR)
        lines = planned["tasks"][0]["plan_lines"]
        assert state == {**intent.state_of(DOC, DOC["tasks"][0], PR), "plan_lines": lines, "plan_chunk": None}
        seen = []
        assert intent.judge(state, decide=chunk_classifier(seen=seen)) == (
            "unchecked",
            f"the plan chunk for lines {lines} could not be read",
        )
        assert seen == []

    def test_a_chunk_truncated_by_the_history_bound_is_unchecked(self):
        state = intent_history.prepare({**CHUNK_STATE, "plan_chunk": "x\n" * 20000})
        assert intent.judge(state, decide=chunk_classifier()) == (
            "unchecked",
            "the plan chunk for lines 15-17 could not be read",
        )

    def test_a_slice_too_long_for_line_questions_is_still_judged_on_the_whole_chunk(self):
        from hooks.classifier.questions import MAX_QUESTIONS

        fits = MAX_QUESTIONS - len(intent.BASE_QUESTIONS) - len(intent.CHUNK_QUESTIONS)
        state = {"plan_lines": f"1-{fits + 1}", "plan_chunk": "".join(f"r{n}\n" for n in range(1, fits + 2))}
        fitting = "".join(f"r{n}\n" for n in range(1, fits + 1))
        assert len(intent.questions_for({"plan_lines": f"1-{fits}", "plan_chunk": fitting})) == MAX_QUESTIONS
        assert list(intent.questions_for(state)) == [*intent.BASE_QUESTIONS, *intent.CHUNK_QUESTIONS]
        verdict, reason = intent.judge(state, decide=chunk_classifier(0.5))
        quoted = ", ".join(f'line {n} "r{n}"' for n in range(1, fits + 2))
        assert (verdict, reason.split("; ")[-1]) == (
            "fail",
            f"What would meet intent: Deliver what plan lines 1-{fits + 1} ask for. No single line was named, so check "
            f"each: {quoted}.",
        )

    def test_the_chunk_questions_are_asked_only_with_a_chunk(self):
        assert [*intent.BASE_QUESTIONS, *intent.CHUNK_QUESTIONS] == [*BASE_QUESTIONS, "underdelivers", "overdelivers"]
        assert list(intent.questions_for({})) == BASE_QUESTIONS
        assert list(intent.questions_for(CHUNK_STATE)) == CHUNK_QUESTIONS
        line = intent.questions_for(CHUNK_STATE)["misses_line_17"]
        assert (line.instructions, line.criteria()) == (
            'Does the change leave out what plan line 17 asks for: "line 17"?',
            {
                "true": "the change leaves out what this line asks for",
                "false": "the change delivers what this line asks for, or the line asks for nothing",
            },
        )
        seen = []
        assert intent.judge({}, decide=chunk_classifier(0.9, 0.9, seen=seen))[0] == "pass"
        assert intent.judge(CHUNK_STATE, decide=chunk_classifier(seen=seen))[0] == "pass"
        assert seen == [BASE_QUESTIONS, CHUNK_QUESTIONS]

    def test_underdelivery_at_one_half_fails_quoting_the_chunk_lines_missed(self):
        assert intent.judge(CHUNK_STATE, decide=chunk_classifier(underdelivers=0.49))[0] == "pass"
        assert intent.judge(CHUNK_STATE, decide=chunk_classifier(0.5, misses={"misses_line_17": 0.5})) == (
            "fail",
            "the phase can use this change at probability 0.90; the change may leave out something plan lines 15-17 "
            "ask for, at probability 0.50; What would meet intent: Deliver what plan lines 15-17 ask for and the "
            'change leaves out: line 17 "line 17".',
        )

    def test_underdelivery_with_no_line_named_quotes_the_whole_chunk(self):
        assert intent.judge(CHUNK_STATE, decide=chunk_classifier(0.5, misses={"misses_line_17": 0.49})) == (
            "fail",
            "the phase can use this change at probability 0.90; the change may leave out something plan lines 15-17 "
            "ask for, at probability 0.50; What would meet intent: Deliver what plan lines 15-17 ask for. No single "
            'line was named, so check each: line 15 "line 15", line 17 "line 17".',
        )

    def test_overdelivery_at_one_half_fails_quoting_the_chunk_lines_exceeded(self):
        assert intent.judge(CHUNK_STATE, decide=chunk_classifier(overdelivers=0.49))[0] == "pass"
        assert intent.judge(CHUNK_STATE, decide=chunk_classifier(overdelivers=0.5)) == (
            "fail",
            "the phase can use this change at probability 0.90; the change may add scope plan lines 15-17 do not "
            "ask for, at probability 0.50; What would meet intent: Remove the scope beyond plan lines 15-17, which "
            'ask only for line 15 "line 15", line 17 "line 17".',
        )

    def test_the_task_remediation_comes_before_the_chunk_steps(self):
        state = {**CHUNK_STATE, "task": "T", "task_text": "Do it.", "phase": "P", "phase_intent": "Goal."}
        reason = intent.judge(state, decide=chunk_classifier(0.7, 0.8, misses={"misses_line_15": 0.9}))[1]
        assert reason.split("; ")[1:] == [
            "the change may leave out something plan lines 15-17 ask for, at probability 0.70",
            "the change may add scope plan lines 15-17 do not ask for, at probability 0.80",
            "What would meet intent: Deliver T: Do it.. The phase must be able to use it for P: Goal.. Deliver what "
            'plan lines 15-17 ask for and the change leaves out: line 15 "line 15". Remove the scope beyond plan '
            'lines 15-17, which ask only for line 15 "line 15", line 17 "line 17".',
        ]

    def test_the_tick_records_the_chunk_questions_it_asked(self, tmp_path, planned):
        def ask(state):
            return intent.judge(state, chunk_classifier())

        check(tmp_path, "observe", ask=ask).run(planned)
        [record] = [json.loads(line) for line in (tmp_path / SLUG / "gates" / "intent" / "history.jsonl").open()]
        assert list(json.loads(record["classifier_input"])["questions"]) == [
            *BASE_QUESTIONS,
            "underdelivers",
            "overdelivers",
            "misses_line_15",
            "misses_line_16",
            "misses_line_17",
        ]


SLICE = "line 15\nline 16\nline 17\n"


class TestPlanSource:
    def test_the_chunk_is_read_from_the_task_plan_url_when_the_phase_has_no_range(self, planned):
        planned["tasks"][0]["plan_url"] = planned["phases"][0].pop("plan_ref")["artifact"]
        assert intent.state_of(planned, planned["tasks"][0], PR)["plan_chunk"] == SLICE

    def test_the_chunk_is_read_from_the_phase_plan_url(self, planned):
        planned["phases"][0]["plan_url"] = planned["phases"][0].pop("plan_ref")["artifact"]
        assert intent.state_of(planned, planned["tasks"][0], PR)["plan_chunk"] == SLICE

    def test_a_swarm_v2_package_slice_is_read_from_the_package_plan(self, monkeypatch):
        from scripts.swarm_ledger import plan_packages

        monkeypatch.setattr(plan_packages, "text", lambda: PLAN)
        issue = "https://github.com/o/r/issues/7"
        task = {**DOC["tasks"][0], "plan_lines": "15-17", "plan_slice": "SV2-CTL-04", "plan_url": issue}
        assert intent.state_of({**DOC, "tasks": [task]}, task, PR)["plan_chunk"] == SLICE

    def test_a_pass_cites_the_plan_lines_it_judged(self):
        assert intent.judge(CHUNK_STATE, decide=chunk_classifier()) == (
            "pass",
            "the phase can use it as delivered at probability 0.90, judged against plan lines 15-17",
        )


TRACED = {
    "plan_hash": "h1",
    "pieces": [
        {
            "what": "read the slice",
            "areas": ["scripts/gates/intent.py", "docs"],
            "why": "the judge needs it",
            "kept": True,
        },
        {"what": "a generator", "areas": ["power"], "why": "it lights the yard", "kept": False},
        {"what": "cite the lines", "areas": ["docs"], "why": "the verdict names them", "kept": True},
    ],
}
UNOPENED = {**DOC, "tasks": [{**{k: v for k, v in DOC["tasks"][0].items() if k != "pr_url"}, "state": "claimed"}]}
PLANNED_REASON = "the phase can use it as delivered at probability 0.80"
JUDGE = intent.judge


def planned_ask(seen, usable=0.8):
    def ask(state):
        seen.append(state)
        return JUDGE(state, classifier(usable))

    return ask


@pytest.fixture
def asks(monkeypatch):
    return lambda answer: monkeypatch.setattr(intent, "judge", answer)


class TestPlanCheck:
    def test_the_traced_plan_is_judged_before_the_pull_request_opens(self, tmp_path, asks):
        seen = []
        asks(planned_ask(seen))
        checked = intent.plan_check(SLUG, UNOPENED, TASK, TRACED, "coach", NOW, home=tmp_path)
        assert checked == {"verdict": "pass", "reason": PLANNED_REASON}
        [state] = seen
        assert state["pull_request_body"] == (
            "- read the slice | scripts/gates/intent.py, docs | the judge needs it\n"
            "- cite the lines | docs | the verdict names them\n"
        )
        assert state["changed_files"] == ["docs", "scripts/gates/intent.py"]
        assert verdicts(tmp_path).read(TASK) == {
            "verdict": "pass",
            "reason": PLANNED_REASON,
            "at": NOW,
            "phase": "p8",
            "planned": "h1",
        }
        [record] = [json.loads(line) for line in (tmp_path / SLUG / "gates" / "intent" / "history.jsonl").open()]
        assert {k: record[k] for k in ("task", "agent", "at", "verdict", "reason", "purpose")} == {
            "task": TASK,
            "agent": ME,
            "at": NOW,
            "verdict": "pass",
            "reason": PLANNED_REASON,
            "purpose": "intent-check",
        }
        assert rows(tmp_path) == []

    def test_the_same_plan_is_judged_once(self, tmp_path, asks):
        seen = []
        asks(planned_ask(seen))
        first, again = (
            intent.plan_check(SLUG, UNOPENED, TASK, TRACED, "coach", at, home=tmp_path) for at in (NOW, NOW + 5)
        )
        assert (len(seen), again, verdicts(tmp_path).read(TASK)["at"]) == (1, first, NOW)

    @pytest.mark.parametrize("doc,mode", [(DOC, "coach"), (UNOPENED, "off"), ({**DOC, "tasks": []}, "coach")])
    def test_an_open_pull_request_a_missing_task_or_the_gate_off_judges_nothing(self, tmp_path, asks, doc, mode):
        seen = []
        asks(planned_ask(seen))
        assert intent.plan_check(SLUG, doc, TASK, TRACED, mode, NOW, home=tmp_path) is None
        assert (seen, verdicts(tmp_path).read(TASK)) == ([], None)

    def test_a_planned_pass_holds_nothing_after_the_first_push(self, tmp_path, asks, stamped):
        asks(planned_ask([]))
        intent.plan_check(SLUG, UNOPENED, TASK, TRACED, "coach", NOW, home=tmp_path)
        assert intent.stamp(SLUG, TASK, URL, DOC, "coach", NOW + 5, home=tmp_path) == {"verdict": "pass", "body": True}
        assert verdicts(tmp_path).read(TASK)["verdict"] == "pass"
        gate = intent.IntentGate(clock=lambda: (NOW + 10) / 1000)
        assert gate.decide(bash("gh pr merge 9"), WHO, verdicts(tmp_path), "coach").allowed

    @pytest.mark.parametrize("mode,kind", [("coach", "observe"), ("observe", "observe"), ("enforce", "deny")])
    def test_a_planned_fail_is_logged_and_armed_pending_when_the_pull_request_opens(
        self, tmp_path, asks, stamped, mode, kind
    ):
        asks(lambda state: ("fail", "the plan misses the slice"))
        intent.plan_check(SLUG, UNOPENED, TASK, TRACED, mode, NOW, home=tmp_path)
        logged = [(r["gate"], r["kind"], r["agent"], r["task"], r["tool"], r["reason"]) for r in rows(tmp_path)]
        assert logged == [("intent", kind, ME, TASK, "", "the plan misses the slice")]
        assert intent.stamp(SLUG, TASK, URL, DOC, mode, NOW + 5, home=tmp_path)["verdict"] == "pending"

    def test_an_unchecked_plan_of_an_unclaimed_task_is_counted_in_the_gate_log(self, tmp_path, asks):
        asks(lambda state: ("unchecked", "no answer"))
        unclaimed = {**UNOPENED, "tasks": [{k: v for k, v in UNOPENED["tasks"][0].items() if k != "claimed_by"}]}
        planned = intent.plan_check(SLUG, unclaimed, TASK, TRACED, "enforce", NOW, home=tmp_path)
        assert planned == {"verdict": "unchecked", "reason": "no answer"}
        logged = [(r["gate"], r["kind"], r["agent"], r["task"], r["tool"], r["reason"]) for r in rows(tmp_path)]
        assert logged == [("intent", "count", "", TASK, "", "no answer")]
        [record] = [json.loads(line) for line in (tmp_path / SLUG / "gates" / "intent" / "history.jsonl").open()]
        assert record["agent"] == ""

    def test_the_tick_still_judges_the_pull_request_after_a_planned_pass(self, tmp_path, asks):
        asks(planned_ask([]))
        intent.plan_check(SLUG, UNOPENED, TASK, TRACED, "enforce", NOW, home=tmp_path)
        asks(JUDGE)
        got = run_pass(tmp_path, usable=0.1)
        assert got.viewed == [URL]
        assert "planned" not in verdicts(tmp_path).read(TASK)
        assert verdicts(tmp_path).read(TASK)["verdict"] == "fail"
        assert not intent.IntentGate().decide(bash("gh pr merge 9"), WHO, verdicts(tmp_path)).allowed


def test_the_pass_keeps_the_tick_after_each_judged_pull_request(tmp_path):
    second = "https://github.com/o/r/pull/10"
    doc = {**DOC, "tasks": [*DOC["tasks"], {**DOC["tasks"][0], "id": "t2", "pr_url": second}]}
    order = []

    def ask(state):
        order.append("judge")
        return intent.judge(state, classifier(0.9))

    keeping = timing.BEFORE_STEP.set(lambda: order.append("keep"))
    try:
        check(tmp_path, view=lambda url: None if url == second else PR, ask=ask).run(doc)
        assert order == ["judge", "keep"]
        check(tmp_path / "again", ask=ask).run(doc)
    finally:
        timing.BEFORE_STEP.reset(keeping)
    assert order[2] == "judge"
    assert sorted(order[2:]) == ["judge", "judge", "keep", "keep"]
