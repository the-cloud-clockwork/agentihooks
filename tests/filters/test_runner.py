import threading

import pytest

from hooks.classifier import Answer, ClassifierError, DecisionResult
from hooks.context import conditions
from hooks.filters import extract, runner, schema

pytestmark = pytest.mark.unit

FILTER = """intent: user facing text names no internal ids
finders:
  - regex: 'task [a-z]+[0-9]+'
    reason: names a task id
"""
SENT_BACK = '[condition pre-write-ids.filter.yaml] filter sent the text back:\n- "task flt1": names a task id'


def _write_call(content: str, mode: str = "default") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "sid-filters",
        "tool_name": "Write",
        "tool_input": {"file_path": "/repo/page.py", "content": content},
        "permission_mode": mode,
        "cwd": "/tmp",
    }


def test_a_confirmed_finding_sends_the_write_back_with_the_finding(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    stub(yes=True)
    effect = conditions.pre_effect(_write_call('label = "waiting on task flt1"'))
    assert effect.block is not None
    assert "task flt1" in effect.block
    assert "names a task id" in effect.block


def test_a_finding_the_classifier_clears_lets_the_write_through(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    stub(yes=False)
    effect = conditions.pre_effect(_write_call('label = "waiting on task flt1"'))
    assert effect.block is None
    assert effect.contexts == []


def test_an_unavailable_classifier_passes_the_text_and_logs_the_pass(filters_dir, stub, monkeypatch):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    stub(error=ClassifierError("no decision backend answered"))
    logged = []
    monkeypatch.setattr("hooks.common.log", lambda message, data=None: logged.append((message, data)))
    effect = conditions.pre_effect(_write_call('label = "waiting on task flt1"'))
    assert effect.block is None
    assert logged == [
        (
            "filter passed: classifier unavailable",
            {
                "filter": str(filters_dir / "pre-write-ids.filter.yaml"),
                "findings": 1,
                "error": "no decision backend answered",
            },
        )
    ]


def test_no_classifier_backend_answering_passes_the_text(filters_dir, monkeypatch):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL", raising=False)
    logged = []
    monkeypatch.setattr("hooks.common.log", lambda message, data=None: logged.append((message, data)))
    effect = conditions.pre_effect(_write_call("see task flt1"))
    assert effect.block is None
    assert [(m, d["error"]) for m, d in logged] == [
        ("filter passed: classifier unavailable", "no decision backend answered")
    ]


def test_questions_the_classifier_refuses_fail_the_filter_as_a_condition(filters_dir, monkeypatch):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL", raising=False)
    effect = conditions.pre_effect(_write_call(" ".join(f"task t{i}" for i in range(129))))
    assert effect.block is None
    assert effect.contexts == [
        "[condition pre-write-ids.filter.yaml] failed "
        "(classifier refused the questions: ask 1 to 128 questions, got 129) — skipped"
    ]


def test_an_unknown_key_fails_the_filter_as_a_condition_and_allows_the_call(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "colour: red\n")
    fake = stub(yes=True)
    effect = conditions.pre_effect(_write_call('label = "waiting on task flt1"'))
    assert effect.block is None
    assert effect.contexts == [
        "[condition pre-write-ids.filter.yaml] failed (invalid filter: unknown keys: colour) — skipped"
    ]
    assert fake.calls == []


def test_two_findings_ask_one_classifier_call_with_two_questions(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    fake = stub(yes=True)
    effect = conditions.pre_effect(_write_call("task flt1 then task flt2"))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert list(call["questions"]) == ["finding_0", "finding_1"]
    assert call["fallbacks"] == []
    assert call["purpose"] == "filter"
    assert call["state"] == {
        "filter": "pre-write-ids.filter.yaml",
        "tool": "Write",
        "path": "/repo/page.py",
        "intent": "user facing text names no internal ids",
    }
    first = call["questions"]["finding_0"]
    assert first.instructions == (
        "Does this finding go against the filter intent?\n"
        "Intent: user facing text names no internal ids\nFinding: task flt1\nReason: names a task id\n"
        "Context: task flt1 then task flt2"
    )
    assert (first.true, first.false) == (runner.TRUE, runner.FALSE)
    assert effect.block == (
        "[condition pre-write-ids.filter.yaml] filter sent the text back:\n"
        '- "task flt1": names a task id\n- "task flt2": names a task id'
    )


TAIL_FILTER = """intent: user facing text states facts without explaining them
finders:
  - regex: '(?:because|since|so that|which means) [^"]*'
    reason: explanation tail
"""
WITH_TAIL = 'line = f"{free} free seats because accounts have quota"'
WITHOUT_TAIL = 'line = f"{free} free seats"'


def test_the_phase_case_an_explanation_tail_is_sent_back_and_the_bare_line_passes(filters_dir, stub):
    (filters_dir / "pre-edit+write+multiedit-explanation_tail.filter.yaml").write_text(TAIL_FILTER)
    stub(yes=True)
    sent_back = conditions.pre_effect(_write_call(WITH_TAIL)).block
    assert sent_back == (
        "[condition pre-edit+write+multiedit-explanation_tail.filter.yaml] filter sent the text back:\n"
        '- "because accounts have quota": explanation tail'
    )
    assert conditions.pre_effect(_write_call(WITHOUT_TAIL)).block is None


def test_the_phase_case_both_lines_pass_with_the_classifier_down(filters_dir, monkeypatch):
    (filters_dir / "pre-edit+write+multiedit-explanation_tail.filter.yaml").write_text(TAIL_FILTER)
    monkeypatch.delenv("AGENTIHOOKS_CLASSIFIER_URL", raising=False)
    assert conditions.pre_effect(_write_call(WITH_TAIL)).block is None
    assert conditions.pre_effect(_write_call(WITHOUT_TAIL)).block is None


def test_only_the_findings_the_classifier_confirms_are_sent_back(filters_dir, monkeypatch):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    answers = {"finding_0": Answer(type="noul", noul=0.2), "finding_1": Answer(type="noul", noul=0.5)}
    monkeypatch.setattr("hooks.classifier.decide", lambda *a, **k: DecisionResult(answers=answers, source="stub"))
    effect = conditions.pre_effect(_write_call("task flt1 then task flt2"))
    assert effect.block == SENT_BACK.replace("flt1", "flt2")


def test_text_without_a_finding_asks_nothing(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER)
    fake = stub(yes=True)
    assert conditions.pre_effect(_write_call("plain words only")).block is None
    assert fake.calls == []


def test_finders_mode_sends_back_without_the_classifier(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "mode: finders\n")
    fake = stub(yes=False)
    assert conditions.pre_effect(_write_call("see task flt1")).block == SENT_BACK
    assert fake.calls == []


def test_classifier_mode_asks_about_the_whole_text(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text("mode: classifier\nintent: no jargon\n")
    fake = stub(yes=True)
    effect = conditions.pre_effect(_write_call("leverage synergies"))
    assert [q.instructions for q in fake.calls[0]["questions"].values()] == [
        "Does this finding go against the filter intent?\nIntent: no jargon\nFinding: leverage synergies\nReason: whole text\n"
        "Context: leverage synergies"
    ]
    assert "leverage synergies" in effect.block


def test_classifier_mode_skips_empty_text(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text("mode: classifier\n")
    fake = stub(yes=True)
    assert conditions.pre_effect(_write_call("")).block is None
    assert fake.calls == []


def test_flag_allows_and_adds_the_findings_as_context(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "action: flag\n")
    stub(yes=True)
    effect = conditions.pre_effect(_write_call("see task flt1"))
    assert effect.block is None
    assert effect.contexts == ['[condition pre-write-ids.filter.yaml]\nfilter flagged:\n- "task flt1": names a task id']


def test_strip_rewrites_the_input_without_the_confirmed_spans(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "action: strip\n")
    stub(yes=True)
    effect = conditions.pre_effect(_write_call("see task flt1 and task flt2 now", mode="bypassPermissions"))
    assert effect.block is None
    assert effect.rewrite == {"file_path": "/repo/page.py", "content": "see  and  now"}
    assert effect.decision == "allow"
    assert (
        '[condition pre-write-ids.filter.yaml]\nfilter stripped:\n- "task flt1": names a task id' in effect.contexts[0]
    )


def test_strip_asks_outside_bypass_permissions(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "action: strip\n")
    stub(yes=True)
    effect = conditions.pre_effect(_write_call("see task flt1"))
    assert effect.rewrite == {"file_path": "/repo/page.py", "content": "see "}
    assert effect.decision == "ask"


def test_strip_in_classifier_mode_removes_the_whole_text(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text("mode: classifier\naction: strip\n")
    stub(yes=True)
    effect = conditions.pre_effect(_write_call("all of it", mode="bypassPermissions"))
    assert effect.rewrite == {"file_path": "/repo/page.py", "content": ""}


def test_strip_rewrites_only_the_edits_with_findings(filters_dir, stub):
    (filters_dir / "pre-multiedit-ids.filter.yaml").write_text(FILTER + "action: strip\n")
    stub(yes=True)
    call = _write_call("x", mode="bypassPermissions")
    edits = [{"old_string": "a", "new_string": "keep"}, {"old_string": "b", "new_string": "drop task flt1 here"}]
    call["tool_name"], call["tool_input"] = "MultiEdit", {"file_path": "/repo/page.py", "edits": edits}
    effect = conditions.pre_effect(call)
    assert effect.rewrite == {
        "file_path": "/repo/page.py",
        "edits": [{"old_string": "a", "new_string": "keep"}, {"old_string": "b", "new_string": "drop  here"}],
    }


def test_strip_sends_back_where_the_harness_cannot_rewrite_input(filters_dir, stub, monkeypatch):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "action: strip\n")
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    stub(yes=True)
    assert conditions.pre_effect(_write_call("see task flt1", mode="bypassPermissions")).block == SENT_BACK


def test_strip_sends_back_after_the_tool_ran():
    spec = schema.parse({"action": "strip", "finders": [{"regex": "flt1"}]})
    findings = runner.find(spec, extract.pieces("Write", {"content": "flt1"}))
    assert runner.strip("post", _write_call("flt1"), findings) == {
        "returncode": 2,
        "stdout": "",
        "stderr": 'filter sent the text back:\n- "flt1": matched a finder',
    }


def test_overlapping_spans_are_cut_once():
    assert runner._cut("abcdefgh", [(4, 6), (1, 3), (2, 5)]) == "agh"


def test_a_long_finding_is_listed_whole():
    finding = runner.Finding(("content",), 0, 201, "b" * 201, "r")
    assert runner._listing([finding]) == f'- "{"b" * 201}": r'


def test_an_empty_match_is_no_finding():
    spec = schema.parse({"finders": [{"regex": "x*"}]})
    assert runner.find(spec, extract.pieces("Write", {"content": "abc"})) == []


@pytest.mark.parametrize("path, applies", [("/repo/page.py", True), ("/repo/page.md", False)])
def test_paths_limit_which_files_a_filter_reads(filters_dir, stub, path, applies):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "paths: ['page.py']\n")
    fake = stub(yes=True)
    call = _write_call("see task flt1")
    call["tool_input"]["file_path"] = path
    assert (conditions.pre_effect(call).block is not None) is applies
    assert len(fake.calls) == int(applies)


def test_a_full_path_glob_matches(filters_dir, stub):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "paths: ['/repo/*']\n")
    stub(yes=True)
    assert conditions.pre_effect(_write_call("see task flt1")).block is not None


def test_paths_skip_a_call_with_no_path(filters_dir, stub):
    (filters_dir / "pre-any-ids.filter.yaml").write_text(FILTER + "paths: ['*']\n")
    fake = stub(yes=True)
    call = _write_call("x")
    call["tool_name"], call["tool_input"] = "mcp__ledger__say", {"text": "see task flt1"}
    assert conditions.pre_effect(call).block is None
    assert fake.calls == []


def test_intent_from_reads_the_intent_from_the_tool_input(filters_dir, stub):
    (filters_dir / "pre-any-ids.filter.yaml").write_text(FILTER + "intent_from: goal\n")
    fake = stub(yes=True)
    call = _write_call("x")
    call["tool_name"], call["tool_input"] = "mcp__ledger__say", {"text": "see task flt1", "goal": "plain words"}
    conditions.pre_effect(call)
    assert fake.calls[0]["state"]["intent"] == "plain words"
    assert fake.calls[0]["state"]["path"] == ""


@pytest.mark.parametrize("goal", [None, "", 7])
def test_intent_from_falls_back_to_the_intent_without_a_text_value(filters_dir, stub, goal):
    (filters_dir / "pre-write-ids.filter.yaml").write_text(FILTER + "intent_from: goal\n")
    fake = stub(yes=True)
    call = _write_call("see task flt1")
    if goal is not None:
        call["tool_input"]["goal"] = goal
    conditions.pre_effect(call)
    assert fake.calls[0]["state"]["intent"] == "user facing text names no internal ids"


def test_a_tool_input_that_is_not_a_mapping_passes(tmp_path):
    path = tmp_path / "pre-any-ids.filter.yaml"
    path.write_text(FILTER)
    entry = {"file": path.name, "path": str(path)}
    assert runner.run(entry, "pre", {"tool_name": "Write", "tool_input": ["task flt1"]}) == {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
    }


def test_a_filter_that_overruns_the_timeout_fails(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(runner, "run", lambda *a: release.wait(5) or {"returncode": 0})
    try:
        result = conditions.execute({"file": "pre-any-slow.filter.yaml", "path": "x"}, "pre", {}, 0.05)
    finally:
        release.set()
    assert result == {"error": "timed out after 0.05s"}


def test_a_crashing_filter_fails_without_raising(monkeypatch):
    def boom(*args):
        raise RuntimeError("bad state")

    monkeypatch.setattr(runner, "run", boom)
    result = conditions.execute({"file": "pre-any-boom.FILTER.yaml", "path": "x"}, "pre", {}, 1)
    assert result == {"error": "filter crashed: bad state"}


def test_a_filter_runs_in_process_and_hands_back_the_runner_result(monkeypatch):
    seen = []
    monkeypatch.setattr(
        runner, "run", lambda *args: seen.append((*args, threading.current_thread().daemon)) or {"returncode": 0}
    )
    monkeypatch.setattr(conditions.subprocess, "Popen", lambda *a, **k: pytest.fail("filters never spawn"))
    entry = {"file": "pre-any-x.filter.yaml", "path": "x"}
    assert conditions.execute(entry, "pre", {"tool_name": "Write"}, 1) == {"returncode": 0}
    assert seen == [(entry, "pre", {"tool_name": "Write"}, True)]
