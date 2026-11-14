from collections.abc import Callable
from typing import Any

import pytest

from scripts.gates import intent
from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger import ledger_workspace
from tests.helpers.seat import seat  # noqa: F401
from tests.swarm.test_cli import env, run  # noqa: F401

ME = "engineer@a1b2c3-0001"
FIRST = "https://github.com/o/r/pull/9"
SECOND = "https://github.com/o/r/pull/10"
FINAL = "https://github.com/o/r/pull/11"


@pytest.fixture
def started(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    seat: Callable[..., AgentRecord],  # noqa: F811
) -> tuple[RedisStore, Any, list[tuple[str, Any, Any]]]:
    """Swarm ``sw`` running with an engineer on t1 and a ci agent on t2 seated through hub admission, their work
    folders written, pull request body stamps recorded and the session acting as the engineer."""
    store, ledger, _ = env
    ledger.phases = [{"id": "p1", "title": "Gates", "description": "Stop failures."}]
    ledger.rows["t1"].update(phase="p1", title="Intent", description="Refuse a failed merge.")
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="running")
    for task_id in ("t1", "t2"):
        seat(task_id)
        ledger_workspace.scaffold("sw", ledger.task("sw", task_id), ledger.state("sw"))
    stamped: list[tuple[str, Any, Any]] = []
    monkeypatch.setattr(intent, "stamp_body", lambda url, doc, task, run=None: stamped.append((url, doc, task)) or True)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ME)
    return store, ledger, stamped


def test_swarm_pr_records_the_scope_each_staged_or_prerequisite_pull_request_delivers(started):  # noqa: F811
    _, ledger, stamped = started
    assert run("sw", "pr", FIRST, "--prerequisite", " tests that accept both gate states ") == 0
    first = {FIRST: {"kind": "prerequisite", "scope": "tests that accept both gate states"}}
    assert (ledger.rows["t1"]["pull_scopes"], ledger.rows["t1"]["pr_url"]) == (first, FIRST)
    assert stamped[-1][2]["pull_scopes"] == first
    assert run("sw", "pr", SECOND, "--stage", "the gate") == 0
    both = {**first, SECOND: {"kind": "stage", "scope": "the gate"}}
    assert ledger.rows["t1"]["pull_scopes"] == both
    assert run("sw", "pr", FINAL) == 0
    assert (ledger.rows["t1"]["pull_scopes"], ledger.rows["t1"]["pr_url"]) == (both, FINAL)


def test_swarm_pr_without_a_declaration_records_no_scope(started):  # noqa: F811
    _, ledger, stamped = started
    assert run("sw", "pr", FIRST) == 0
    assert "pull_scopes" not in ledger.rows["t1"]
    assert "pull_scopes" not in stamped[-1][2]


@pytest.mark.parametrize("kind", ["stage", "prerequisite"])
def test_swarm_pr_refuses_a_blank_scope(started, capsys, kind):  # noqa: F811
    _, ledger, stamped = started
    assert run("sw", "pr", FIRST, f"--{kind}", "  ") != 0
    assert capsys.readouterr().err.strip().endswith(f"--{kind} needs the scope this pull request delivers")
    assert stamped == [] and not ledger.rows["t1"].get("pr_url")


def test_a_pull_request_declares_one_kind_only(started, capsys):  # noqa: F811
    with pytest.raises(SystemExit):
        cli.main(["sw", "pr", FIRST, "--stage", "a", "--prerequisite", "b"])
    assert "argument --prerequisite: not allowed with argument --stage" in capsys.readouterr().err


def test_recording_a_declared_pull_request_without_a_declaration_drops_only_its_own(started):  # noqa: F811
    _, ledger, stamped = started
    assert run("sw", "pr", FIRST, "--stage", "the gate") == 0
    assert run("sw", "pr", SECOND, "--prerequisite", "the tests") == 0
    assert run("sw", "pr", FIRST) == 0
    kept = {SECOND: {"kind": "prerequisite", "scope": "the tests"}}
    assert ledger.rows["t1"]["pull_scopes"] == kept
    assert stamped[-1][2]["pull_scopes"] == kept


def test_swarm_pr_refuses_a_declaration_on_a_url_that_is_no_pull_request(started, capsys):  # noqa: F811
    _, ledger, stamped = started
    other = "https://example.com/o/r/pull/9"
    assert run("sw", "pr", other, "--stage", "the gate") != 0
    assert capsys.readouterr().err.strip().endswith(f"--stage needs a GitHub pull request URL, not {other}")
    assert stamped == [] and not ledger.rows["t1"].get("pr_url")


def test_a_prerequisite_recorded_after_the_main_pull_request_keeps_the_main_one(started, monkeypatch):  # noqa: F811
    _, ledger, _ = started
    monkeypatch.setattr(cli, "pull_branch", lambda url: (f"head-{url[-2:]}", "o/r"))
    assert run("sw", "pr", FINAL) == 0
    assert run("sw", "pr", FIRST, "--prerequisite", "the announcement") == 0
    row = ledger.rows["t1"]
    assert (row["pr_url"], row["branch"]) == (FINAL, "head-11")
    assert row["pull_scopes"] == {FIRST: {"kind": "prerequisite", "scope": "the announcement"}}
