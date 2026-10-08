import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from hooks.context import conditions  # noqa: E402
from hooks.filters import check  # noqa: E402
from scripts.inbox import cli  # noqa: E402
from scripts.inbox.store import InboxStore  # noqa: E402
from scripts.swarm_ledger import ledger_agent_ops, ledger_core  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

FILTER = """mode: finders
finders:
  - regex: 'because accounts have quota'
    reason: explains the change
"""
TEXT = "The capacity line changed because accounts have quota"
FINDING = '- "because accounts have quota": explains the change'


def _sent_back(name):
    return f"[condition {name}] filter sent the text back:\n{FINDING}"


def _comment(text=TEXT):
    return {"op": "add", "id": "c1", "thread": "phases/p1/comments", "text": text, "by": "eng"}


def _followup(text=TEXT):
    return {"op": "add_item", "id": "f1", "list": "followups", "text": text, "by": "eng"}


def test_a_matching_ledger_write_filter_refuses_an_agent_comment_with_the_finding(project_filters):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(FILTER)
    with pytest.raises(ValueError) as refused:
        ledger_core.check_op(_comment())
    assert str(refused.value) == _sent_back("pre-ledger_write-x.filter.yaml")


@pytest.mark.parametrize("kind", ["followups", "questions"])
def test_a_matching_ledger_write_filter_refuses_a_follow_up_or_question(project_filters, kind):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(FILTER)
    with pytest.raises(ValueError) as refused:
        ledger_agent_ops.check({**_followup(), "list": kind})
    assert str(refused.value) == _sent_back("pre-ledger_write-x.filter.yaml")


def test_agent_chat_and_operator_comments_are_not_filtered(project_filters):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(FILTER)
    operator = _comment()
    del operator["by"]
    assert ledger_core.check_op(operator) is None
    assert ledger_core.check_op({**_comment(), "thread": "chat"}) is None


def test_with_no_filters_ledger_writes_pass_unchanged(project_filters):
    assert ledger_core.check_op(_comment()) is None
    assert ledger_agent_ops.check(_followup()) is None
    assert check.check("ledger_write", {"text": TEXT}) == check.FilterOutcome("allow")


def test_a_filter_for_another_synthetic_tool_leaves_ledger_writes_alone(project_filters):
    (project_filters / "pre-inbox_send-x.filter.yaml").write_text(FILTER)
    assert ledger_core.check_op(_comment()) is None
    assert ledger_agent_ops.check(_followup()) is None
    assert check.check("inbox_send", {"text": TEXT}) == check.FilterOutcome(
        "deny", _sent_back("pre-inbox_send-x.filter.yaml")
    )


def test_an_internal_error_allows_and_logs(project_filters, monkeypatch):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(FILTER)

    def broken(step, payload):
        raise RuntimeError("index unreadable")

    logged = []
    monkeypatch.setattr(conditions, "run_step", broken)
    monkeypatch.setattr("hooks.common.log", lambda message, data=None: logged.append((message, data)))
    assert check.check("ledger_write", {"text": TEXT}) == check.FilterOutcome("allow")
    assert ledger_core.check_op(_comment()) is None
    assert logged == [("filter check failed open", {"tool": "ledger_write", "error": "index unreadable"})] * 2


def test_the_check_runs_the_pre_step_with_the_synthetic_tool_and_the_working_folder(project_filters, monkeypatch):
    seen = []
    monkeypatch.setattr(conditions, "run_step", lambda step, payload: seen.append((step, payload)))
    assert check.check("judge", {"text": "case"}, cwd="/somewhere") == check.FilterOutcome("allow")
    check.check("judge", {"text": "case"})

    def payload(cwd):
        return {
            "hook_event_name": "PreToolUse",
            "session_id": "",
            "tool_name": "judge",
            "tool_input": {"text": "case"},
            "cwd": cwd,
        }

    assert seen == [("pre", payload("/somewhere")), ("pre", payload(str(project_filters.parent.parent)))]


def test_a_strip_filter_returns_the_rewritten_input(project_filters):
    (project_filters / "pre-judge-x.filter.yaml").write_text(FILTER + "action: strip\n")
    outcome = check.check("judge", {"text": TEXT, "case": "c"})
    assert outcome == check.FilterOutcome("rewrite", "", {"text": "The capacity line changed ", "case": "c"})


def test_a_flag_filter_allows(project_filters):
    (project_filters / "pre-judge-x.filter.yaml").write_text(FILTER + "action: flag\n")
    assert check.check("judge", {"text": TEXT}) == check.FilterOutcome("allow")


def test_an_ask_without_a_rewrite_is_refused(monkeypatch):
    result = conditions.StepResult(decisions=["ask"], reasons=["[condition x] confirm", "[condition y] again"])
    monkeypatch.setattr(conditions, "run_step", lambda step, payload: result)
    assert check.check("judge", {"text": TEXT}) == check.FilterOutcome(
        "deny", "[condition x] confirm\n[condition y] again"
    )


def test_an_allow_with_a_rewrite_is_a_rewrite(monkeypatch):
    result = conditions.StepResult(decisions=["allow"], input_patch={"text": "short"}, reasons=["[condition x] cut"])
    monkeypatch.setattr(conditions, "run_step", lambda step, payload: result)
    assert check.check("judge", {"text": TEXT, "case": "c"}) == check.FilterOutcome(
        "rewrite", "[condition x] cut", {"text": "short", "case": "c"}
    )


def test_screen_returns_the_text_raises_on_a_deny_and_applies_a_rewrite(monkeypatch):
    seen = []
    outcomes = iter([check.FilterOutcome("allow"), check.FilterOutcome("rewrite", "", {"text": "cut"})])

    def fake(tool, tool_input):
        seen.append((tool, tool_input))
        return next(outcomes)

    monkeypatch.setattr(check, "check", fake)
    assert check.screen("ledger_write", TEXT) == TEXT
    assert check.screen("inbox_send", TEXT) == "cut"
    assert seen == [("ledger_write", {"text": TEXT}), ("inbox_send", {"text": TEXT})]
    monkeypatch.setattr(check, "check", lambda tool, tool_input: check.FilterOutcome("deny", "sent back"))
    with pytest.raises(ValueError) as refused:
        check.screen("ledger_write", TEXT)
    assert str(refused.value) == "sent back"


def test_screen_refuses_a_rewrite_that_leaves_no_text(monkeypatch):
    monkeypatch.setattr(check, "check", lambda tool, tool_input: check.FilterOutcome("rewrite", "", {"text": " \n"}))
    with pytest.raises(ValueError) as refused:
        check.screen("ledger_write", TEXT)
    assert str(refused.value) == "a filter stripped all of the text"


def test_a_strip_filter_that_removes_a_whole_comment_refuses_it(project_filters):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(
        "mode: finders\nfinders:\n  - regex: '.+'\n    reason: all of it\naction: strip\n"
    )
    for write in (lambda: ledger_core.check_op(_comment()), lambda: ledger_agent_ops.check(_followup())):
        with pytest.raises(ValueError) as refused:
            write()
        assert str(refused.value) == "a filter stripped all of the text"


def test_a_matching_ledger_write_filter_refuses_an_agent_status_comment(project_filters):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(FILTER)
    op = {"op": "set", "id": "s1", "path": "phases/p1/done", "value": True, "status": TEXT, "by": "eng"}
    with pytest.raises(ValueError) as refused:
        ledger_agent_ops.check(op)
    assert str(refused.value) == _sent_back("pre-ledger_write-x.filter.yaml")


def test_a_strip_filter_rewrites_ledger_and_inbox_text_before_it_lands(project_filters, inbox, capsys):
    for tool in ("ledger_write", "inbox_send"):
        (project_filters / f"pre-{tool}-x.filter.yaml").write_text(FILTER + "action: strip\n")
    comment, followup = _comment(), _followup()
    status = {"op": "set", "id": "s1", "path": "phases/p1/done", "value": True, "status": TEXT, "by": "eng"}
    assert ledger_core.check_op(comment) is None
    assert ledger_agent_ops.check(followup) is None
    assert ledger_agent_ops.check(status) is None
    assert (comment["text"], followup["text"], status["status"]) == ("The capacity line changed ",) * 3
    assert cli.main(["send", "bob", *TEXT.split()]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert inbox.get(sent["id"]).text == "The capacity line changed "


@pytest.fixture
def inbox(monkeypatch):
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "registered_name", lambda: "", raising=False)
    monkeypatch.setattr(
        "scripts.inbox.addresses.get_active_sessions",
        lambda **kwargs: {"sess-a": {"name": "alice"}, "sess-b": {"name": "bob"}},
    )
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "alice")
    return store


def test_a_matching_inbox_send_filter_refuses_the_send_with_the_finding(project_filters, inbox, capsys):
    (project_filters / "pre-inbox_send-x.filter.yaml").write_text(FILTER)
    assert cli.main(["send", "bob", *TEXT.split()]) == 1
    assert capsys.readouterr().err == f"agentihooks msg: {_sent_back('pre-inbox_send-x.filter.yaml')}\n"
    assert inbox.mailbox("bob") == []


def test_with_no_filters_an_inbox_send_lands(project_filters, inbox, capsys):
    assert cli.main(["send", "bob", *TEXT.split()]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert inbox.get(sent["id"]).text == TEXT


class _CaptureMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


def test_condition_list_names_the_synthetic_tools_and_reports_a_misspelled_one(project_filters):
    from hooks.mcp import conditions as mcp_conditions

    (project_filters / "pre-ledger_writ-x.filter.yaml").write_text(FILTER)
    (project_filters / "pre-write+inbox_send-y.filter.yaml").write_text(FILTER)
    capture = _CaptureMCP()
    mcp_conditions.register(capture)
    listed = json.loads(capture.tools["condition_list"]())
    assert listed["synthetic_tools"] == ["judge", "ledger_write", "inbox_send"]
    assert listed["invalid"] == [
        {
            "path": str(project_filters / "pre-ledger_writ-x.filter.yaml"),
            "source": "directory",
            "error": "unknown tool 'ledger_writ': did you mean the synthetic tool 'ledger_write'?",
        }
    ]
    assert [c["file"] for c in listed["conditions"]] == [
        "pre-ledger_writ-x.filter.yaml",
        "pre-write+inbox_send-y.filter.yaml",
    ]


def test_misspelled_names_only_tool_alternatives_close_to_a_synthetic_name():
    entry = {"path": "/c/pre-x.sh", "source": "bundle"}
    assert conditions.misspelled({**entry, "matcher": "write+bash.git+mcp+judge"}) is None
    assert conditions.misspelled({**entry, "matcher": "edit+judg"}) == {
        "path": "/c/pre-x.sh",
        "source": "bundle",
        "error": "unknown tool 'judg': did you mean the synthetic tool 'judge'?",
    }
    assert conditions.misspelled({**entry, "matcher": "inbox_sent"})["error"] == (
        "unknown tool 'inbox_sent': did you mean the synthetic tool 'inbox_send'?"
    )
