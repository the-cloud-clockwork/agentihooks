import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger, ledger_agent_ops, ledger_core  # noqa: E402
from scripts.swarm_ledger.api import schemas  # noqa: E402
from scripts.swarm_ledger.api.errors import APIError  # noqa: E402

FILTER = """mode: finders
finders:
  - regex: 'because accounts have quota'
    reason: explains the change
"""
TEXT = "The capacity line changed because accounts have quota"
SENT_BACK = (
    "[condition pre-ledger_write-x.filter.yaml] filter sent the text back:\n"
    '- "because accounts have quota": explains the change'
)
UNTRUSTED = "the writer's worktree is outside the operator's trusted repositories"


@pytest.fixture
def server_elsewhere(project_filters, tmp_path, monkeypatch):
    (project_filters / "pre-ledger_write-x.filter.yaml").write_text(FILTER)
    server = tmp_path / "server"
    server.mkdir()
    monkeypatch.chdir(server)
    return str(project_filters.parent.parent)


def _comment(worktree, text=TEXT):
    return {"op": "add", "id": "c1", "thread": "phases/p1/comments", "text": text, "by": "eng", "worktree": worktree}


def _followup(worktree, text=TEXT):
    return {"op": "add_item", "id": "f1", "list": "followups", "text": text, "by": "eng", "worktree": worktree}


def test_a_project_filter_refuses_a_comment_written_from_its_repo_while_the_server_runs_elsewhere(server_elsewhere):
    with pytest.raises(ValueError) as refused:
        ledger_core.check_op(_comment(server_elsewhere))
    assert str(refused.value) == SENT_BACK


def test_a_project_filter_refuses_a_follow_up_written_from_a_subfolder_of_its_repo(server_elsewhere):
    inner = Path(server_elsewhere) / "src"
    inner.mkdir()
    with pytest.raises(ValueError) as refused:
        ledger_agent_ops.check(_followup(str(inner)))
    assert str(refused.value) == SENT_BACK


def test_the_server_folder_layer_no_longer_applies_without_the_writer_worktree(server_elsewhere):
    assert ledger_core.check_op({k: v for k, v in _comment(server_elsewhere).items() if k != "worktree"}) is None
    assert ledger_core.check_op(_comment(server_elsewhere, "The capacity line changed")) is None
    assert ledger_agent_ops.check(_followup(server_elsewhere, "The capacity line changed")) is None


def test_a_worktree_in_a_repository_the_operator_does_not_trust_is_refused(server_elsewhere, tmp_path, monkeypatch):
    monkeypatch.setattr("hooks.config.CONDITIONS_TRUSTED_OWNERS", "")
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=stranger, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:stranger/x.git"], cwd=stranger, check=True)
    for check, op in ((ledger_core.check_op, _comment), (ledger_agent_ops.check, _followup)):
        with pytest.raises(ValueError) as refused:
            check(op(str(stranger), "A plain line"))
        assert str(refused.value) == UNTRUSTED


@pytest.mark.parametrize("worktree", ["relative/path", "", 7])
def test_a_worktree_must_be_an_absolute_path(server_elsewhere, worktree):
    for check, op in ((ledger_core.check_op, _comment), (ledger_agent_ops.check, _followup)):
        with pytest.raises(ValueError) as refused:
            check(op(worktree, "A plain line"))
        assert str(refused.value) == "worktree must be the writer's absolute folder path"


def test_a_worktree_rides_only_on_an_agent_comment_or_follow_up(server_elsewhere):
    chat = {**_comment(server_elsewhere, "A plain line"), "thread": "chat"}
    question = {**_followup(server_elsewhere, "A plain line"), "list": "questions"}
    for check, op in ((ledger_core.check_op, chat), (ledger_agent_ops.check, question)):
        with pytest.raises(ValueError) as refused:
            check(op)
        assert str(refused.value) == "worktree rides only on an agent comment or follow up"


def test_the_api_schema_takes_the_worktree_and_screens_with_it(server_elsewhere):
    for op in (_comment(server_elsewhere), _followup(server_elsewhere)):
        assert schemas.mismatched_field(schemas.operation_schema(op["op"]), op) is None
        with pytest.raises(APIError) as refused:
            schemas.check_operations({"operation_id": "o1", "ops": [op], "guards": {}}, ledger_core, ())
        assert (refused.value.status, str(refused.value)) == (
            400,
            f"Operation does not match its domain schema: {SENT_BACK}",
        )


def test_the_comment_and_follow_up_commands_send_the_writers_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    sent = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops: sent.extend(ops) or {})
    base = ["--slug", "demo", "--as", "eng"]
    ledger.cmd_comment(ledger.build_parser().parse_args([*base, "comment", "tasks/t1", "Done."]))
    ledger.cmd_followup(ledger.build_parser().parse_args([*base, "followup", "add", "Retry the build"]))
    assert [op["worktree"] for op in sent] == [str(tmp_path), str(tmp_path)]
