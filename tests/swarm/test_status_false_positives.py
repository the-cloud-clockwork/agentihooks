from typing import Any

import pytest

from scripts.swarm import cli
from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run


@pytest.mark.feature("swarm-health-false-positives")
def test_status_counts_each_detector_s_false_positive_verdicts(
    env: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run("sw", "create", "--repo", "/repo")
    finding = cli.health.Finding("stale claim", "t1", "no progress for 40 minutes", ("quiet",), "30 minutes", 40)
    monkeypatch.setattr(cli.health, "findings", lambda *a: [finding])
    run("sw", "status")
    capsys.readouterr()
    run("sw", "--as", "master@a1b2c3-0001", "verdict", "stale-claim/t1", "false-positive", "--note", "on checks")
    capsys.readouterr()
    run("sw", "status")
    assert "false positives  stale-claim 1 of 1 judged" in capsys.readouterr().out.splitlines()
