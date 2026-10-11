import subprocess

import pytest

from scripts.swarm import provider_probe

pytestmark = pytest.mark.unit


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
    if harness != "copilot":
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
