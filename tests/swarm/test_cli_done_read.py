import json

from scripts.swarm import cli
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
