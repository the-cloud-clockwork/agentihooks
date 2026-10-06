import json
import os

import pytest

from scripts.gates.progress import Progress
from scripts.gates.verdicts import Verdicts
from scripts.swarm import cli, status
from scripts.swarm.store import AgentRecord
from scripts.swarm_ledger import ledger_workspace
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

ME = "engineer@a1b2c3-0001"
MIN = 60_000


@pytest.fixture
def started(env, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli, "now_ms", lambda: 1_000)
    return store, ledger


def test_progress_reports_the_status_line_and_clears_the_quiet_flag(started, capsys):
    store, ledger = started
    Verdicts("sw", "quiet").write(ME, "quiet", "quiet for 30 minutes")
    capsys.readouterr()
    assert run("sw", "--as", ME, "progress", "--doing", "writing tests", "--ends-when", "they pass") == 0
    line = "Doing writing tests. Done when they pass."
    assert json.loads(capsys.readouterr().out) == {"agent": ME, "task": "t1", "progress": line}
    assert ledger.comments == [("t1", line, ME)]
    assert (ledger_workspace.folder("sw", "t1") / "progress.md").read_text().endswith(f" {line}\n")
    assert Progress(store.redis, "sw").read(ME).outcome_at == 1_000
    assert Verdicts("sw", "quiet").read(ME) is None


def test_progress_needs_both_lines_and_a_worker(started, capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "progress", "--doing", "x"])
    capsys.readouterr()
    assert run("sw", "--as", "master@a1b2c3-0001", "progress", "--doing", "x", "--ends-when", "y") == 1
    assert "master@a1b2c3-0001" in capsys.readouterr().err


def test_status_findings_flag_a_claim_quiet_past_the_limit(started, monkeypatch):
    store, ledger = started
    [agent] = [a for a in store.agents("sw") if a.name == ME]
    store.put_agent("sw", AgentRecord(**{**agent.__dict__, "started_at": MIN}))
    os.utime(ledger_workspace.folder("sw", "t1") / "progress.md", (0, 0))
    monkeypatch.setattr(status, "now_ms", lambda: 45 * MIN)
    tasks = [{"id": "t1", "state": "claimed", "claimed_by": ME}]
    found = status.findings(store, "sw", store.config("sw"), tasks, [])
    stale = [(f["subject"], f["summary"]) for f in found if f["kind"] == "stale claim"]
    assert stale == [("t1", "no progress for 44 minutes")]
    Progress(store.redis, "sw").outcome(ME, "pushed", 40 * MIN)
    found = status.findings(store, "sw", store.config("sw"), tasks, [])
    assert [f for f in found if f["kind"] == "stale claim"] == []
