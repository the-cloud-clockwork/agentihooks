import json

import pytest

from scripts.swarm import cli, done_gate
from scripts.swarm.ledger_events import view as read_view
from tests.forge.fakes import Sent
from tests.forge.test_gitlab import LINK, answer
from tests.helpers.seat import SLUG, seat_agent
from tests.swarm.graph_port import view as read_pull_request
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_pull_views import merged_with_empty_check_entries


def test_done_on_a_merged_pull_request_read_without_its_rollup_names_the_fallback(env, monkeypatch, capsys):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    seat_agent(store, ledger, SLUG, "t1")
    seat_agent(store, ledger, SLUG, "t2")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    fallback = cli.ledger_events.ROLLUP_FALLBACK
    pull = cli.ledger_events.PullRequest("MERGED", 1, None, False, fallback=fallback)
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pull)
    capsys.readouterr()
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "task": "t1",
        "state": "done",
        "pull_request": fallback,
        "next": "stop now; the swarm closes this session",
    }
    assert ledger.rows["t1"]["state"] == "done"


def test_done_on_a_fully_read_pull_request_prints_no_fallback(env, monkeypatch, capsys):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    seat_agent(store, ledger, SLUG, "t1")
    seat_agent(store, ledger, SLUG, "t2")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    pull = cli.ledger_events.PullRequest("MERGED", 1, None, False, resolved=True)
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: pull)
    capsys.readouterr()
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "task": "t1",
        "state": "done",
        "next": "stop now; the swarm closes this session",
    }


def test_done_closes_a_task_whose_merged_pull_request_has_empty_check_entries(env, monkeypatch, capsys):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    seat_agent(store, ledger, SLUG, "t1")
    seat_agent(store, ledger, SLUG, "t2")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    github = merged_with_empty_check_entries()
    monkeypatch.setattr(cli.ledger_events, "view", lambda url: read_pull_request(url, github))
    capsys.readouterr()
    assert run("sw", "done", "--pr", "https://github.com/o/r/pull/9") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["state"] == "done"
    assert ledger.rows["t1"]["state"] == "done"


def _gitlab_done(env, monkeypatch, state):  # noqa: F811
    """Seat t1 and t2 and read the recorded GitLab merge request in ``state`` through the real view."""
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    seat_agent(store, ledger, SLUG, "t1")
    seat_agent(store, ledger, SLUG, "t2")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setenv("GITLAB_TOKEN", "fake-token")
    monkeypatch.setattr("scripts.swarm.gitlab_pull.TRANSPORT", Sent(answer(state, "success"), answer(state, "success")))
    monkeypatch.setattr(cli.ledger_events, "view", read_view)
    return ledger


@pytest.mark.feature("swarm-gitlab-change-close")
def test_done_closes_a_task_on_its_merged_gitlab_merge_request(env, monkeypatch, capsys):  # noqa: F811
    ledger = _gitlab_done(env, monkeypatch, "merged")
    capsys.readouterr()
    assert run("sw", "done", "--pr", LINK) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {
        "task": "t1",
        "state": "done",
        "next": "stop now; the swarm closes this session",
    }
    assert ledger.rows["t1"]["state"] == "done"


@pytest.mark.feature("swarm-gitlab-change-close")
def test_done_refuses_a_task_whose_gitlab_merge_request_is_open(env, monkeypatch, capsys):  # noqa: F811
    ledger = _gitlab_done(env, monkeypatch, "opened")
    capsys.readouterr()
    assert run("sw", "done", "--pr", LINK) == 1
    assert capsys.readouterr().err == f"swarm: pull request {LINK} is open, not merged; {done_gate.MERGE}\n"
    assert ledger.rows["t1"]["state"] != "done"
