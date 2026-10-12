import subprocess

import pytest

from scripts.swarm import provider_probe

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _codex_account(monkeypatch):
    import shutil
    import uuid

    monkeypatch.setenv("AH_CX_TOKEN_alpha", uuid.uuid4().hex)
    monkeypatch.setattr(shutil, "which", lambda name: "codex")


@pytest.mark.parametrize(
    ("harness", "output"),
    [
        ("claude", '{"type":"result","is_error":false,"result":"OK"}'),
        (
            "codex",
            '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n{"type":"turn.completed"}',
        ),
        ("copilot", "OK"),
    ],
)
def test_probe_checks_a_model_response_on_the_existing_account_route(tmp_path, harness, output):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, output, "")

    account = "default" if harness == "copilot" else "alpha"
    result = provider_probe.check(provider_probe.Target(harness, account, "model"), tmp_path, run=run)
    assert result.ok
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs["input"] == "Reply with exactly OK."
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 30
    if harness == "claude":
        at = args.index("--route")
        assert args[at + 1] == "alpha"
        assert args[at - 1] == harness
    assert "model" in args


@pytest.mark.parametrize(
    ("harness", "output", "code"),
    [
        ("claude", '{"type":"result","is_error":true,"result":"error"}', 0),
        ("claude", "invalid output", 0),
        ("codex", '{"type":"turn.failed"}', 0),
        ("codex", '{"type":"turn.completed"}', 0),
        ("copilot", "service unavailable", 0),
        ("claude", '{"type":"result","is_error":false,"result":"OK"}', 1),
    ],
)
def test_failed_or_incomplete_probe_cannot_claim_recovery(tmp_path, harness, output, code):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, code, output, "")

    assert not provider_probe.check(provider_probe.Target(harness, "default", "model"), tmp_path, run=run).ok


def test_probe_backoff_is_per_account_and_capped_at_ten_minutes():
    states = {"claude:alpha": provider_probe.schedule(provider_probe.Target("claude", "alpha", "model"), 1000)}
    calls = []

    def unavailable(target):
        calls.append(target.account)
        return provider_probe.Result(False, "request failed")

    assert not provider_probe.advance(states, 60999, unavailable)
    assert calls == []
    for at, next_at in [(61000, 181000), (181000, 421000), (421000, 901000), (901000, 1501000), (1501000, 2101000)]:
        assert not provider_probe.advance(states, at, unavailable)
        assert states["claude:alpha"]["next_at"] == next_at
    assert calls == ["alpha"] * 5
    assert provider_probe.advance(states, 2101000, lambda target: provider_probe.Result(True, "request succeeded"))
    assert states["claude:alpha"]["succeeded_at"] == 2101000
    assert provider_probe.advance(states, 2701000, unavailable)
    assert calls == ["alpha"] * 5


@pytest.mark.parametrize(
    ("harness", "output"),
    [
        (
            "claude",
            '[agenti] account=alpha route=forced\n{\n  "type": "result",\n  "is_error": false,\n  "result": "OK"\n}',
        ),
        (
            "codex",
            '[agentihooks codex] account=alpha sessions=1/6 placement=forced\n{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n{"type":"turn.completed"}',
        ),
    ],
)
def test_probe_accepts_existing_router_banners_and_multiline_json(tmp_path, harness, output):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, output, "")

    assert provider_probe.check(provider_probe.Target(harness, "alpha", "model"), tmp_path, run=run).ok


@pytest.mark.parametrize("timeout", [False, True])
def test_default_probe_transport_bounds_and_reaps_its_process_group(tmp_path, monkeypatch, timeout):
    import signal

    killed = []
    options = []

    class Child:
        pid = 123
        returncode = 0

        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def communicate(self, value=None, timeout=None):
            self.calls += 1
            if self.calls == 1 and fail:
                raise subprocess.TimeoutExpired("probe", timeout)
            return ('{"type":"result","is_error":false,"result":"OK"}', "")

    fail = timeout
    child = Child()

    def popen(args, **kwargs):
        options.append(kwargs)
        return child

    monkeypatch.setattr(provider_probe.subprocess, "Popen", popen)
    monkeypatch.setattr(provider_probe.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    result = provider_probe.check(provider_probe.Target("claude", "alpha", "model"), tmp_path)
    assert result.ok is not timeout
    assert options[0]["start_new_session"]
    assert killed == ([(123, signal.SIGKILL)] if timeout else [])
    assert child.calls == (2 if timeout else 1)


def test_probe_records_completion_time_instead_of_request_start_time():
    states = {"claude:alpha": provider_probe.schedule(provider_probe.Target("claude", "alpha", "model"), 1000)}
    assert provider_probe.advance(states, 61000, lambda target: provider_probe.Result(True, "succeeded", 70000))
    assert states["claude:alpha"]["succeeded_at"] == 70000
