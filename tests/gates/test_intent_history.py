import json

import pytest

from hooks.classifier.result import DecisionRequest
from scripts.gates import intent
from scripts.gates.verdicts import Verdicts


def document():
    return {
        "overview": "Make gates measurable.",
        "phases": [{"id": "p", "title": "Gates", "description": "Calibrate from real history."}],
        "tasks": [
            {
                "id": "t",
                "title": "Retain inputs",
                "description": "Keep the original inputs and findings.",
                "phase": "p",
                "state": "pr",
                "pr_url": "https://github.com/o/r/pull/1",
                "claimed_by": "engineer",
            }
        ],
    }


def pull_request():
    return {
        "title": "Retain intent history",
        "body": "Proof: replay passed.\nUnicode: español",
        "files": ["scripts/gates/intent.py"],
        "reviewer_findings": {
            "reviews": [{"author": {"login": "reader"}, "body": "Retain the original findings.", "state": "APPROVED"}],
            "comments": [{"body": "The replay assertion is byte for byte."}],
            "inline": [{"body": "Keep masking before bounds.", "path": "scripts/gates/intent.py", "line": 3}],
        },
    }


def history(home):
    return [
        json.loads(line) for line in (home / "proof" / "gates" / "intent" / "history.jsonl").read_text().splitlines()
    ]


@pytest.mark.parametrize("verdict", ["pass", "fail", "unchecked"])
def test_each_verdict_replays_the_exact_request_and_original_findings(tmp_path, verdict):
    seen = []
    pr = pull_request()

    def ask(state):
        seen.append(json.dumps(DecisionRequest(state, intent.QUESTIONS).wire()).encode())
        return verdict, "original result"

    check = intent.Check("proof", "observe", 123, None, None, lambda url: pr, ask, tmp_path)
    assert check.run(document()) == [f"task t intent check {verdict}"]
    [record] = history(tmp_path)
    assert record["classifier_input"].encode() == seen[0]
    replay = json.loads(record["classifier_input"])
    assert replay["state"]["reviewer_findings"] == pr["reviewer_findings"]
    assert record["verdict"] == verdict
    assert record["reason"] == "original result"
    assert record["at"] == 123
    assert record["task"] == "t"
    assert record["agent"] == "engineer"
    assert record["purpose"] == "intent-check"
    Verdicts("proof", "intent", tmp_path).clear("t")
    pr["body"] = "A different revision"
    pr["reviewer_findings"] = {"reviews": []}
    check.run(document())
    original, changed = history(tmp_path)
    assert original == record
    assert changed["classifier_input"].encode() == seen[1]
    assert changed["classifier_input"] != original["classifier_input"]


@pytest.mark.parametrize("mode", ["off", "standard"])
def test_secrets_are_masked_before_the_classifier_and_history(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("SECRETS_MODE", mode)
    token = "ghp_" + "a" * 36
    pr = pull_request()
    pr["body"] = "token " + token
    pr["reviewer_findings"]["reviews"][0]["body"] = "token " + token
    doc = document()
    doc["tasks"][0]["proof"] = {"password": "synthetic value", "output": ["token " + token]}
    seen = []
    intent.Check(
        "proof",
        "observe",
        123,
        None,
        None,
        lambda url: pr,
        lambda state: seen.append(state) or ("pass", "ok"),
        tmp_path,
    ).run(doc)
    [record] = history(tmp_path)
    assert token not in record["classifier_input"]
    assert "synthetic value" not in record["classifier_input"]
    assert json.loads(record["classifier_input"])["state"] == seen[0]
    assert seen[0]["proof"]["password"] == "[REDACTED]"
    assert "[REDACTED:github_token]" in seen[0]["pull_request_body"]
    assert "[REDACTED:github_token]" in seen[0]["reviewer_findings"]["reviews"][0]["body"]


def test_large_inputs_and_findings_are_bounded_before_classification(tmp_path):
    pr = pull_request()
    pr["body"] = "é\n" * 100000
    pr["reviewer_findings"]["reviews"][0]["body"] = '\\"' * 100000
    seen = []
    intent.Check(
        "proof",
        "observe",
        123,
        None,
        None,
        lambda url: pr,
        lambda state: seen.append(state) or ("pass", "ok"),
        tmp_path,
    ).run(document())
    [record] = history(tmp_path)
    assert len(record["classifier_input"].encode()) <= 131072
    replay = json.loads(record["classifier_input"])["state"]
    assert replay == seen[0]
    assert replay["pull_request_body"]["truncated"] is True
    assert replay["reviewer_findings"]["truncated"] is True
    assert len(json.dumps(replay["pull_request_body"]).encode()) <= 8192
    assert len(json.dumps(replay["reviewer_findings"]).encode()) <= 32768


def test_pull_request_findings_include_original_reviews_and_inline_comments():
    calls = []
    review = {"author": {"login": "spec"}, "body": "The exact input was missing.", "state": "CHANGES_REQUESTED"}
    inline = {
        "user": {"login": "standards"},
        "body": "Bound the retained input.",
        "path": "scripts/gates/intent.py",
        "line": 4,
    }
    discussion = {"author": {"login": "author"}, "body": "Both findings are fixed."}

    def run(args, **kwargs):
        from subprocess import CompletedProcess

        calls.append(args)
        payload = {"title": "Retention", "body": "", "files": [], "reviews": [review], "comments": [discussion]}
        return CompletedProcess(args, 0, stdout=json.dumps(payload if len(calls) == 1 else [[inline]]))

    pr = intent.pr_view("https://github.com/o/r/pull/1", run=run)
    assert pr["reviewer_findings"] == {"reviews": [review], "comments": [discussion], "inline": [inline]}
    assert calls[0][-1] == "title,body,files,reviews,comments"
    assert calls[1] == ["gh", "api", "--paginate", "--slurp", "repos/o/r/pulls/1/comments"]


@pytest.mark.parametrize("result", ["failed", "malformed"])
def test_unavailable_review_findings_leave_the_check_pending(tmp_path, result):
    from subprocess import CompletedProcess

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return CompletedProcess(args, 0, stdout=json.dumps({"title": "Retention", "body": "", "files": []}))
        return CompletedProcess(args, 1 if result == "failed" else 0, stdout="not json")

    asked = []
    check = intent.Check(
        "proof",
        "observe",
        123,
        None,
        None,
        lambda url: intent.pr_view(url, run=run),
        lambda state: asked.append(state) or ("pass", "ok"),
        tmp_path,
    )
    assert check.run(document()) == []
    assert asked == []
    assert Verdicts("proof", "intent", tmp_path).read("t")["verdict"] == "pending"
    assert not (tmp_path / "proof" / "gates" / "intent" / "history.jsonl").exists()


def test_each_field_limit_keeps_exact_boundary_and_marks_truncation():
    from scripts.gates import intent_history

    state = {"exact": "a" * 8190, "over": "a" * 8191, "proof": {"count": 2, "checked": True, "missing": None}}
    safe = intent_history.prepare(state)
    assert safe["exact"] == state["exact"]
    assert safe["over"]["truncated"] is True
    assert safe["over"]["json_prefix"] == json.dumps(state["over"])[:4064]
    assert safe["proof"] == state["proof"]


def test_invalid_pull_request_url_cannot_be_judged():
    assert (
        intent.pr_view("https://example.com/pull/1", run=lambda *args, **kwargs: pytest.fail("unexpected call")) is None
    )


def test_detected_private_key_removes_the_entire_value(monkeypatch):
    from scripts.gates import intent_history

    monkeypatch.setattr(intent_history, "redact", lambda text, mode: "[REDACTED:private_key]\nkey material")
    assert intent_history.masked("synthetic private key") == "[REDACTED:private_key]"
