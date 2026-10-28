import subprocess
from unittest.mock import Mock

import pytest

from scripts.swarm import github_reads


@pytest.mark.parametrize("failed", [False, True])
def test_paginated_responses_charge_each_page(tmp_path, monkeypatch, failed):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    endpoint = "repos/example/project/actions/runs/1/jobs"
    headers = "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n"
    output = headers + '{"id":1}\n' + headers + '{"id":2}\n'
    result = subprocess.CompletedProcess([], 0, output, "diagnostic")
    run = Mock(return_value=result)
    if failed:
        run.side_effect = subprocess.CalledProcessError(1, [], output=output, stderr="HTTP 403")
    args = ["api", endpoint, "--paginate", "--jq", ".jobs[]"]
    if failed:
        with pytest.raises(subprocess.CalledProcessError) as error:
            github_reads.rest("caller", args, run, capture_output=True, text=True, check=True)
        assert error.value.stdout == '{"id":1}\n{"id":2}\n'
        assert error.value.stderr == "HTTP 403"
    else:
        observed = github_reads.rest("caller", args, run, capture_output=True, text=True, check=True)
        assert observed is result
        assert observed.stdout == '{"id":1}\n{"id":2}\n'
        assert observed.stderr == "diagnostic"
    assert "--include" in run.call_args.args[0]
    assert github_reads.counts(0, float("inf")) == {"caller": 2}


@pytest.mark.parametrize(
    "args", [["api", "graphql", "-f", "query={viewer{login}}"], ["repo", "view", "--json", "name"]]
)
def test_non_rest_commands_do_not_charge_core_quota(tmp_path, monkeypatch, args):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    result = subprocess.CompletedProcess([], 0, "{}", "")
    run = Mock(return_value=result)
    assert github_reads.rest("caller", args, run, capture_output=True, text=True) is result
    run.assert_called_once_with(["gh", *args], capture_output=True, text=True)
    assert github_reads.counts(0, float("inf")) == {}
