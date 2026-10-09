import json
import subprocess

import pytest

from scripts.swarm import idle, waits
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.unit
URL = "https://github.com/org/repo/actions/runs/123"
ME = "sw-eng-1"


def test_cli_wait_binds_the_branch_preflight_run(env, monkeypatch):  # noqa: F811
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    ledger.tasks = lambda slug: list(ledger.rows.values())
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "path": ".github/workflows/mutation-preflight.yml",
                    "event": "push",
                    "head_sha": "first",
                    "status": "in_progress",
                }
            ),
        ),
    )
    assert run("sw", "--as", ME, "wait", "--on", "mutation", URL) == 0
    assert idle.wait(store.redis, "sw", ME)["on"] == {
        "kind": "mutation",
        "target": URL,
        "head": "first",
    }
    assert waits.resolution({"kind": "mutation", "target": URL, "head": "first"}, {}, None, None, None, False) == ""
