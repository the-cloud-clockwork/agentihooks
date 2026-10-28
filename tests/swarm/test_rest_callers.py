import json
import subprocess
from unittest.mock import Mock

import pytest

from scripts.swarm import dev_red, github_reads, merge_path, promotion_watch
from scripts.swarm.health import merge_stall


@pytest.mark.parametrize("caller", ["swarm.merge_path", "swarm.promotion_watch", "swarm.dev_red", "swarm.merge_stall"])
@pytest.mark.parametrize("failed", [False, True])
def test_rest_callers_charge_the_shared_log(tmp_path, monkeypatch, caller, failed):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    endpoint = "repos/example/project/actions/runs"
    output = "{}" if caller == "swarm.merge_stall" else ""
    run = Mock(return_value=subprocess.CompletedProcess([], 0, output, ""))
    if failed:
        run.side_effect = subprocess.CalledProcessError(1, ["gh", "api", endpoint], stderr="HTTP 403")
    if caller == "swarm.merge_path":

        def invoke():
            return merge_path._gh(["api", endpoint], str(tmp_path), run)
    elif caller == "swarm.promotion_watch":

        def invoke():
            return promotion_watch._read(str(tmp_path), endpoint, ".jobs[]", run)
    elif caller == "swarm.dev_red":
        endpoint = dev_red.ENDPOINT

        def invoke():
            return dev_red.latest(str(tmp_path), run)
    else:
        monkeypatch.setattr(merge_stall.subprocess, "run", run)

        def invoke():
            return merge_stall.request(str(tmp_path), ["api", endpoint])

    if failed:
        with pytest.raises((subprocess.CalledProcessError, merge_path.SwarmError)):
            invoke()
    else:
        invoke()
    assert run.call_count == 1
    assert github_reads.counts(0, float("inf")) == {caller: 1}
    rows = [json.loads(line) for line in (github_reads.home() / "charged.jsonl").read_text().splitlines()]
    assert [row["endpoint"] for row in rows] == [endpoint]
    assert run.call_args.kwargs["cwd"] == str(tmp_path)
    if caller == "swarm.merge_stall":
        assert run.call_args.kwargs["timeout"] == 20


def test_failing_test_log_read_is_charged_to_promotion_watch(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    log = "FAILED tests/test_example.py::test_case - AssertionError\n"
    run = Mock(return_value=subprocess.CompletedProcess([], 0, log, ""))
    job = {
        "id": 7,
        "name": f"{promotion_watch.SHARD_JOB} (3.12, 1)",
        "steps": [{"name": promotion_watch.TEST_STEP, "conclusion": "failure"}],
    }
    assert promotion_watch.failing_tests(str(tmp_path), job, run) == {"tests/test_example.py::test_case"}
    assert github_reads.counts(0, float("inf")) == {"swarm.promotion_watch": 1}


def test_merge_stall_caps_one_read_at_twenty_seconds(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(merge_stall.subprocess, "run", run)
    token = merge_stall.DEADLINE.set(merge_stall.time.monotonic() + 600)
    try:
        merge_stall.request(str(tmp_path), ["api", "repos/example/project/pulls/1"])
    finally:
        merge_stall.DEADLINE.reset(token)
    assert run.call_args.kwargs["timeout"] == 20
