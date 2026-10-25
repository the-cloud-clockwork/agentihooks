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
        seen.append(json.dumps(DecisionRequest(state, intent.questions_for(state)).wire()).encode())
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

        if args[-1] == ".head.sha":
            return CompletedProcess(args, 0, stdout="head")
        calls.append(args)
        payload = {"title": "Retention", "body": "", "files": [], "reviews": [review], "comments": [discussion]}
        pages = f"{json.dumps(inline)}\n{json.dumps({**inline, 'line': 9})}\n"
        return CompletedProcess(args, 0, stdout=json.dumps(payload) if len(calls) == 1 else pages)

    pr = intent.pr_view("https://github.com/o/r/pull/1", run=run)
    assert pr["reviewer_findings"] == {
        "reviews": [review],
        "comments": [discussion],
        "inline": [inline, {**inline, "line": 9}],
    }
    assert calls[0][-1] == "title,body,files,reviews,comments,isDraft,state"
    assert calls[1] == ["gh", "api", "--paginate", "--jq", ".[] | @json", "repos/o/r/pulls/1/comments"]


@pytest.mark.parametrize("result", ["failed", "malformed"])
def test_unavailable_review_findings_leave_the_check_pending(tmp_path, result):
    from subprocess import CompletedProcess

    calls = []

    def run(args, **kwargs):
        if args[-1] == ".head.sha":
            return CompletedProcess(args, 0, stdout="head")
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


def test_opaque_bearer_and_token_fields_are_masked_in_request_and_history(tmp_path):
    doc = document()
    doc["tasks"][0]["proof"] = {"refresh_token": "synthetic refresh", "token": "synthetic token"}
    pr = pull_request()
    pr["reviewer_findings"]["comments"][0]["body"] = "Authorization: Bearer synthetic_secret"
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
    assert json.loads(record["classifier_input"])["state"] == seen[0]
    for value in ("synthetic refresh", "synthetic token", "synthetic_secret"):
        assert value not in record["classifier_input"]
    assert seen[0]["proof"] == {"refresh_token": "[REDACTED]", "token": "[REDACTED]"}
    assert seen[0]["reviewer_findings"]["comments"][0]["body"] == "Authorization: Bearer [REDACTED]"


def test_history_append_creates_missing_directories(tmp_path):
    from scripts.gates import intent_history

    intent_history.append("proof", {"verdict": "pass"}, tmp_path)
    assert history(tmp_path) == [{"verdict": "pass"}]


def test_strict_masking_covers_dictionary_keys_values_and_mixed_case(monkeypatch):
    from scripts.gates import intent_history

    monkeypatch.setattr("hooks.config.SECRETS_MODE", "off")
    token = "xoxb-" + "z" * 20
    safe = intent_history.prepare(
        {
            "proof": {token: ["value " + token], "Refresh_Token": "synthetic refresh"},
            "reviewer_findings": "authorization: bearer short",
        }
    )
    assert safe["proof"] == {"[REDACTED:slack_token]": ["value [REDACTED:slack_token]"], "Refresh_Token": "[REDACTED]"}
    assert safe["reviewer_findings"] == "authorization: Bearer [REDACTED]"


def test_findings_limit_preserves_exact_boundary_and_truncates_the_next_byte():
    from scripts.gates import intent_history

    exact = "a" * 32766
    assert intent_history.prepare({"reviewer_findings": exact})["reviewer_findings"] == exact
    over = intent_history.prepare({"reviewer_findings": exact + "a"})["reviewer_findings"]
    assert over == {"truncated": True, "json_prefix": json.dumps(exact + "a")[:16352]}
    assert len(json.dumps(over)) <= 32768


def test_plan_chunk_limit_matches_the_findings_limit():
    from scripts.gates import intent_history

    exact = "a" * 32766
    assert intent_history.prepare({"plan_chunk": exact})["plan_chunk"] == exact
    over = intent_history.prepare({"plan_chunk": exact + "a"})["plan_chunk"]
    assert over == {"truncated": True, "json_prefix": json.dumps(exact + "a")[:16352]}
