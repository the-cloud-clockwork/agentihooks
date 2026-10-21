import json

from scripts.swarm import cli
from tests.swarm.test_cli import env, run  # noqa: F401


def test_done_on_a_merged_pull_request_read_without_its_rollup_names_the_fallback(env, monkeypatch, capsys):  # noqa: F811
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    fallback = cli.ledger_events.ROLLUP_FALLBACK
    pull = cli.ledger_events.PullRequest("MERGED", 1, None, False, resolved=True, fallback=fallback)
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
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
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
