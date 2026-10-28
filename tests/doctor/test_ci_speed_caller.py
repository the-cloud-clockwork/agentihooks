import json
import subprocess
from unittest.mock import Mock

import pytest

from scripts.doctor import ci_speed_read
from scripts.swarm import github_reads


@pytest.mark.parametrize("retry", [False, True])
def test_speed_requests_charge_each_attempt_without_charging_cached_responses(tmp_path, monkeypatch, retry):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    endpoint = "repos/example/project/actions/runs"
    response = {"workflow_runs": []}
    result = subprocess.CompletedProcess([], 0, json.dumps(response), "")
    run = Mock(return_value=result)
    if retry:
        run.side_effect = [subprocess.CalledProcessError(1, [], stderr="HTTP 503"), result]
    api = ci_speed_read._Api(run, 10, 60, responses={})
    assert api.get(endpoint) == response
    assert api.get(endpoint) == response
    expected = 2 if retry else 1
    assert run.call_count == expected
    assert github_reads.counts(0, float("inf")) == {"doctor.ci_speed": expected}
    rows = [json.loads(line) for line in (github_reads.home() / "charged.jsonl").read_text().splitlines()]
    assert [row["endpoint"] for row in rows] == [endpoint] * expected
