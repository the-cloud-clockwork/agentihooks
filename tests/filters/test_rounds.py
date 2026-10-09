import json
from unittest.mock import Mock

import pytest

from hooks.context import conditions
from hooks.filters import runner

pytestmark = pytest.mark.unit


@pytest.fixture
def filtered(tmp_path, monkeypatch):
    monkeypatch.setattr(conditions, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    path = tmp_path / "pre-write-tail.filter.yaml"
    path.write_text("mode: finders\nfinders:\n  - regex: because accounts have quota\n    reason: explanation tail\n")
    return {"path": str(path), "file": path.name}


def write_call(text="because accounts have quota", session="session", path="/repo/page.py"):
    return {
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": path, "content": text},
        "cwd": "/repo",
    }


def test_three_identical_send_backs_then_pass_with_flag(filtered, monkeypatch):
    log = Mock()
    monkeypatch.setattr("hooks.common.log", log)
    payload = write_call()
    for _ in range(3):
        assert runner.run(filtered, "pre", payload)["returncode"] == 2
    result = runner.run(filtered, "pre", payload)
    assert result["returncode"] == 0
    assert json.loads(result["stdout"])["context"] == (
        'filter flagged: passed after 3 send-backs\n- "because accounts have quota": explanation tail'
    )
    assert log.call_args.args[0] == "filter flagged: round cap reached"
    assert log.call_args.args[1]["rounds"] == 3
    assert log.call_args.args[1]["filter"] == filtered["path"]
    assert log.call_args.args[1]["target"] == "/repo/page.py"
    assert log.call_args.args[1]["session_id"] == "session"
    assert log.call_args.args[1]["findings"] == [{"text": "because accounts have quota", "reason": "explanation tail"}]


def test_clean_pass_resets_only_its_target(filtered):
    for _ in range(3):
        assert runner.run(filtered, "pre", write_call())["returncode"] == 2
        assert runner.run(filtered, "pre", write_call(path="/repo/other.py"))["returncode"] == 2
    assert runner.run(filtered, "pre", write_call("changed 9m ago")) == {"returncode": 0, "stdout": "", "stderr": ""}
    assert runner.run(filtered, "pre", write_call())["returncode"] == 2
    assert runner.run(filtered, "pre", write_call(path="/repo/other.py"))["returncode"] == 0


def test_exhausted_rounds_comment_on_current_swarm_task(filtered, monkeypatch):
    comment = Mock()
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.comment", comment)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "filter-task")
    for _ in range(3):
        runner.run(filtered, "pre", write_call())
    assert not comment.called
    assert runner.run(filtered, "pre", write_call())["returncode"] == 0
    comment.assert_called_once_with(
        "proof-swarm",
        "filter-task",
        "Filter pre write tail passed after 3 send backs. Findings: because accounts have quota: explanation tail.",
        by="swarm",
    )


@pytest.mark.parametrize("maximum", [1, 2, 4])
def test_filter_can_set_its_cap(filtered, maximum):
    from pathlib import Path

    path = Path(filtered["path"])
    path.write_text(path.read_text() + f"max_rounds: {maximum}\n")
    for _ in range(maximum):
        assert runner.run(filtered, "pre", write_call())["returncode"] == 2
    result = runner.run(filtered, "pre", write_call())
    assert result["returncode"] == 0
    assert f"passed after {maximum} send-backs" in json.loads(result["stdout"])["context"]
    assert runner.run(filtered, "pre", write_call())["returncode"] == 0


@pytest.mark.parametrize("identity", ["session", "filter", "file"])
def test_counts_are_independent(filtered, identity):
    from pathlib import Path

    for _ in range(3):
        runner.run(filtered, "pre", write_call())
    payload = write_call()
    other = dict(filtered)
    if identity == "session":
        payload["session_id"] = "other-session"
    elif identity == "filter":
        path = Path(filtered["path"]).with_name("other.filter.yaml")
        path.write_text(Path(filtered["path"]).read_text())
        other = {"path": str(path), "file": path.name}
    else:
        payload["tool_input"]["file_path"] = "/repo/other.py"
    assert runner.run(other, "pre", payload)["returncode"] == 2
    assert runner.run(filtered, "pre", write_call())["returncode"] == 0


def test_counter_persists_in_runtime_directory(filtered, monkeypatch):
    import importlib

    from hooks.filters import rounds

    runner.run(filtered, "pre", write_call())
    directory = conditions.runtime_dir() / "filters" / "rounds"
    paths = list(directory.iterdir())
    assert len(paths) == 1
    assert paths[0].read_text() == "1"
    importlib.reload(rounds)
    for _ in range(2):
        assert runner.run(filtered, "pre", write_call())["returncode"] == 2
    assert runner.run(filtered, "pre", write_call())["returncode"] == 0
    assert paths[0].read_text() == "3"


@pytest.mark.parametrize("missing", ["AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK"])
def test_both_swarm_variables_are_required(filtered, monkeypatch, missing):
    comment = Mock()
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.comment", comment)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "filter-task")
    monkeypatch.delenv(missing)
    for _ in range(4):
        result = runner.run(filtered, "pre", write_call())
    assert result["returncode"] == 0
    assert not comment.called


def test_ledger_failure_preserves_flagged_pass(filtered, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "filter-task")
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.comment", Mock(side_effect=RuntimeError("offline")))
    log = Mock()
    monkeypatch.setattr("hooks.common.log", log)
    for _ in range(4):
        result = runner.run(filtered, "pre", write_call())
    assert result["returncode"] == 0
    assert "passed after 3 send-backs" in json.loads(result["stdout"])["context"]
    assert log.call_args.args == ("filter ledger comment failed", {"error": "offline"})


def test_synthetic_tool_and_content_key_have_separate_counts(filtered):
    payload = {
        "session_id": "session",
        "tool_name": "ledger_write",
        "tool_input": {"text": "because accounts have quota"},
    }
    for _ in range(3):
        assert runner.run(filtered, "pre", payload)["returncode"] == 2
    assert runner.run(filtered, "pre", payload)["returncode"] == 0
    payload["tool_name"] = "inbox_send"
    assert runner.run(filtered, "pre", payload)["returncode"] == 2
    payload["tool_name"] = "ledger_write"
    payload["tool_input"] = {"evidence": ["because accounts have quota"]}
    assert runner.run(filtered, "pre", payload)["returncode"] == 2
    payload["tool_input"] = {"text": "changed 9m ago", "evidence": ["because accounts have quota"]}
    assert runner.run(filtered, "pre", payload)["returncode"] == 2
    payload["tool_input"] = {"text": "because accounts have quota"}
    assert runner.run(filtered, "pre", payload)["returncode"] == 2


def test_synthetic_session_uses_agent_identity(filtered, monkeypatch):
    payload = {"tool_name": "ledger_write", "tool_input": {"text": "because accounts have quota"}}
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "first-agent")
    for _ in range(3):
        assert runner.run(filtered, "pre", payload)["returncode"] == 2
    assert runner.run(filtered, "pre", payload)["returncode"] == 0
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "second-agent")
    assert runner.run(filtered, "pre", payload)["returncode"] == 2


def test_relative_and_absolute_file_paths_share_counts(filtered):
    for _ in range(3):
        assert runner.run(filtered, "pre", write_call(path="page.py"))["returncode"] == 2
    assert runner.run(filtered, "pre", write_call())["returncode"] == 0


def test_mixed_synthetic_targets_show_exhausted_findings_while_others_send_back(filtered):
    payload = {
        "session_id": "session",
        "tool_name": "ledger_write",
        "tool_input": {"text": "because accounts have quota"},
    }
    for _ in range(3):
        runner.run(filtered, "pre", payload)
    payload["tool_input"]["evidence"] = ["because accounts have quota"]
    result = runner.run(filtered, "pre", payload)
    assert result["returncode"] == 2
    assert result["stderr"] == (
        'filter sent the text back:\n- "because accounts have quota": explanation tail\n'
        'filter flagged: passed after 3 send-backs\n- "because accounts have quota": explanation tail'
    )


def test_conditions_surface_fourth_write_as_flag_context(filtered, filters_dir):
    from pathlib import Path

    (filters_dir / "pre-write-tail.filter.yaml").write_text(Path(filtered["path"]).read_text())
    for _ in range(3):
        assert conditions.pre_effect(write_call()).block is not None
    effect = conditions.pre_effect(write_call())
    assert effect.block is None
    assert effect.contexts == [
        "[condition pre-write-tail.filter.yaml]\nfilter flagged: passed after 3 send-backs\n"
        '- "because accounts have quota": explanation tail'
    ]


def test_classifier_cleared_target_resets_rounds(filtered, stub):
    from pathlib import Path

    path = Path(filtered["path"])
    path.write_text(path.read_text().replace("mode: finders", "mode: both"))
    stub(yes=True)
    for _ in range(3):
        assert runner.run(filtered, "pre", write_call())["returncode"] == 2
    stub(yes=False)
    assert runner.run(filtered, "pre", write_call()) == {"returncode": 0, "stdout": "", "stderr": ""}
    stub(yes=True)
    assert runner.run(filtered, "pre", write_call())["returncode"] == 2


def test_multiple_findings_on_one_file_count_as_one_round(filtered):
    payload = write_call("because accounts have quota and because accounts have quota")
    for _ in range(3):
        assert runner.run(filtered, "pre", payload)["returncode"] == 2
    result = runner.run(filtered, "pre", payload)
    assert result["returncode"] == 0
    assert json.loads(result["stdout"])["context"] == (
        'filter flagged: passed after 3 send-backs\n- "because accounts have quota": explanation tail\n'
        '- "because accounts have quota": explanation tail'
    )


def test_strip_fallback_send_backs_are_capped(filtered, monkeypatch):
    from pathlib import Path

    path = Path(filtered["path"])
    path.write_text(path.read_text() + "action: strip\n")
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    for _ in range(3):
        assert runner.run(filtered, "pre", write_call())["returncode"] == 2
    result = runner.run(filtered, "pre", write_call())
    assert result["returncode"] == 0
    assert "passed after 3 send-backs" in json.loads(result["stdout"])["context"]


def test_swarm_comment_accepts_finding_paths_and_code_names(filtered, monkeypatch):
    from pathlib import Path

    from scripts.swarm_ledger.ledger_comments import check

    path = Path(filtered["path"])
    path.write_text("mode: finders\nfinders:\n  - regex: /repo/page.py\n    reason: code_name\n")
    accepted = []

    def comment(self, slug, task, text, by):
        check(text, "comment")
        accepted.append(text)

    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.comment", comment)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "filter-task")
    for _ in range(4):
        result = runner.run(filtered, "pre", write_call("/repo/page.py"))
    assert result["returncode"] == 0
    assert accepted == ["Filter pre write tail passed after 3 send backs. Findings: /repo/flagged text: flagged text."]
    assert json.loads(result["stdout"])["context"] == (
        'filter flagged: passed after 3 send-backs\n- "/repo/page.py": code_name'
    )


def test_missing_session_and_agent_are_reported_as_empty(filtered, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    log = Mock()
    monkeypatch.setattr("hooks.common.log", log)
    payload = write_call(session="")
    for _ in range(4):
        result = runner.run(filtered, "pre", payload)
    assert result["returncode"] == 0
    assert log.call_args.args[1]["session_id"] == ""


def test_multiple_synthetic_targets_have_separate_flag_contexts(filtered):
    payload = {
        "session_id": "session",
        "tool_name": "ledger_write",
        "tool_input": {"text": "because accounts have quota", "evidence": ["because accounts have quota"]},
    }
    for _ in range(3):
        assert runner.run(filtered, "pre", payload)["returncode"] == 2
    result = runner.run(filtered, "pre", payload)
    assert result["returncode"] == 0
    assert json.loads(result["stdout"])["context"] == (
        'filter flagged: passed after 3 send-backs\n- "because accounts have quota": explanation tail\n'
        'filter flagged: passed after 3 send-backs\n- "because accounts have quota": explanation tail'
    )


def test_comment_lists_multiple_findings(filtered, monkeypatch):
    comment = Mock()
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.comment", comment)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "filter-task")
    payload = write_call("because accounts have quota and because accounts have quota")
    for _ in range(4):
        result = runner.run(filtered, "pre", payload)
    assert result["returncode"] == 0
    comment.assert_called_once_with(
        "proof-swarm",
        "filter-task",
        "Filter pre write tail passed after 3 send backs. Findings: because accounts have quota: explanation tail. "
        "because accounts have quota: explanation tail.",
        by="swarm",
    )


@pytest.mark.parametrize(
    "text, rendered",
    [
        ("some-text", "some text"),
        ("(some) (text)", "some) text)"),
        ("some;text;", "some text "),
    ],
)
def test_comment_removes_forbidden_punctuation(filtered, monkeypatch, text, rendered):
    from pathlib import Path

    from scripts.swarm_ledger.ledger_comments import check

    path = Path(filtered["path"])
    path.write_text("mode: finders\nfinders:\n  - regex: .+\n    reason: finding\n")
    comment = Mock()
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.comment", comment)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "filter-task")
    for _ in range(4):
        runner.run(filtered, "pre", write_call(text))
    expected = f"Filter pre write tail passed after 3 send backs. Findings: {rendered}: finding."
    assert comment.call_args.args[2] == expected
    check(expected, "comment")


def test_long_comment_fits_ledger_word_limit(filtered, monkeypatch):
    from pathlib import Path

    from scripts.swarm_ledger.ledger_comments import check

    path = Path(filtered["path"])
    path.write_text("mode: finders\nfinders:\n  - regex: .+\n    reason: finding\n")
    comment = Mock()
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.comment", comment)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "proof-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "filter-task")
    for _ in range(4):
        runner.run(filtered, "pre", write_call("word " * 80))
    text = comment.call_args.args[2]
    assert len(text.split()) == 50
    assert text == "Filter pre write tail passed after 3 send backs. Findings: " + " ".join(["word"] * 40)
    check(text, "comment")
