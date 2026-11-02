import json
import subprocess
from unittest.mock import Mock

import pytest

from scripts.swarm import github_reads


def charged_endpoints():
    return [json.loads(line)["endpoint"] for line in (github_reads.home() / "charged.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("failed", [False, True])
def test_paginated_responses_charge_each_page(tmp_path, monkeypatch, failed):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    endpoint = "repos/example/project/actions/runs/1/jobs"
    headers = "HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n"
    output = headers + '{"id":1}\n\n' + headers + '{"id":2}\n'
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
    assert run.call_args.args[0] == ["gh", *args, "--include"]
    assert github_reads.counts(0, float("inf")) == {"caller": 2}
    assert charged_endpoints() == [endpoint, endpoint]


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


@pytest.mark.parametrize("include", [False, True])
def test_paginated_timeout_preserves_partial_bytes(tmp_path, monkeypatch, include):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    headers = b"HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n"
    output = headers + b'{"id":1}\n\n' + headers + b'{"id":2}\n'
    run = Mock(side_effect=subprocess.TimeoutExpired(["gh"], 10, output=output, stderr=b"timeout"))
    args = ["api", "repos/example/project/actions/jobs", "--paginate", "--jq", ".jobs[]"]
    if include:
        args.append("--include")
    with pytest.raises(subprocess.TimeoutExpired) as error:
        github_reads.rest("caller", args, run, capture_output=True, text=True)
    assert error.value.stdout == (output if include else b'{"id":1}\n{"id":2}\n')
    assert error.value.stderr == b"timeout"
    assert github_reads.counts(0, float("inf")) == {"caller": 2}
    assert run.call_args.args[0] == (["gh", *args] if include else ["gh", *args, "--include"])
    assert charged_endpoints() == [args[1], args[1]]


@pytest.mark.parametrize("body", [b'{"id":1}\n', b""])
@pytest.mark.parametrize("include", [False, True])
def test_timeout_before_next_page_headers_charges_attempt(tmp_path, monkeypatch, body, include):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    output = b"HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + body + b"\n"
    run = Mock(side_effect=subprocess.TimeoutExpired(["gh"], 10, output=output))
    args = ["api", "repos/example/project/actions/jobs", "--paginate", "--jq", ".jobs[]"]
    if include:
        args.append("-i")
    with pytest.raises(subprocess.TimeoutExpired) as error:
        github_reads.rest("caller", args, run, capture_output=True, text=True)
    assert error.value.stdout == (output if include else body)
    assert github_reads.counts(0, float("inf")) == {"caller": 2}
    assert charged_endpoints() == [args[1], args[1]]


def test_single_rest_request_runs_gh_once_and_charges_it(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    result = subprocess.CompletedProcess([], 0, "{}", "")
    run = Mock(return_value=result)
    args = ["api", "repos/example/project/pulls/1"]
    assert github_reads.rest("caller", args, run, capture_output=True, text=True) is result
    run.assert_called_once_with(["gh", *args], capture_output=True, text=True)
    assert charged_endpoints() == [args[1]]


@pytest.mark.parametrize("include", [False, True])
def test_completed_pagination_keeps_a_trailing_blank_line(tmp_path, monkeypatch, include):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    output = 'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"id":1}\n\n'
    run = Mock(return_value=subprocess.CompletedProcess([], 0, output, ""))
    args = ["api", "repos/example/project/actions/jobs", "--paginate"]
    if include:
        args.append("--include")
    observed = github_reads.rest("caller", args, run, capture_output=True, text=True)
    assert observed.stdout == (output if include else '{"id":1}\n\n')
    assert charged_endpoints() == [args[1]]


def test_failed_text_pagination_charges_the_pending_page(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    output = 'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n\r\n{"id":1}\n\n'
    run = Mock(side_effect=subprocess.CalledProcessError(1, [], output=output))
    args = ["api", "repos/example/project/actions/jobs", "--paginate"]
    with pytest.raises(subprocess.CalledProcessError) as error:
        github_reads.rest("caller", args, run, capture_output=True, text=True)
    assert error.value.stdout == '{"id":1}\n'
    assert charged_endpoints() == [args[1], args[1]]
