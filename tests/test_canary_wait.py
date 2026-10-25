import json
from pathlib import Path

import pytest

from scripts import init_agent
from scripts.profiles import binding


def _launch(tmp_path, monkeypatch, target, extra):
    from scripts import select_profile

    home = tmp_path / "engineer" / target
    home.mkdir(parents=True)
    monkeypatch.setattr(
        select_profile,
        "prepare",
        lambda *a: ({"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES[target]: str(home)}, a[4]),
    )
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: (target, "explicit"))
    monkeypatch.setattr(init_agent.claude_trust, "ensure_trusted", lambda *a: ("trusted", ""))
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *a: True)
    request = binding.request
    monkeypatch.setattr(binding, "request", lambda report, profile, agent, home: request(report, profile, agent))

    def launch(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        init_agent._route_report(launcher).write_text("status=routed\naccount=routed-account\n")
        return []

    waited = []

    def wait(report, timeout):
        waited.append(timeout)
        return {"account": "routed-account"}

    monkeypatch.setattr(init_agent, "_start_herdr", launch)
    monkeypatch.setattr(binding, "wait", wait)
    monkeypatch.setattr(init_agent, "_detach", lambda: False)
    argv = ["--host", "herdr", "--agent", target, "--profile", "engineer", "--dir", str(tmp_path)]
    result = init_agent.main([*argv, "--route-timeout", "30", *extra], {"XDG_RUNTIME_DIR": str(tmp_path / "rt")})
    assert result == 0
    return waited


@pytest.mark.parametrize(("target", "seconds"), [("claude", 120.0), ("codex", 90.0)])
def test_the_canary_wait_uses_the_harness_deadline_not_the_route_timeout(tmp_path, monkeypatch, target, seconds):
    assert _launch(tmp_path, monkeypatch, target, []) == [seconds]


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_canary_timeout_flag_overrides_the_harness_deadline(tmp_path, monkeypatch, target):
    assert _launch(tmp_path, monkeypatch, target, ["--canary-timeout", "7"]) == [7.0]


def test_the_canary_timeout_help_names_both_harness_defaults():
    (action,) = [a for a in init_agent._parser()._actions if "--canary-timeout" in a.option_strings]
    assert action.help == (
        "Seconds to wait for the new session to run its profile canary; default 120 for Claude, 90 for Codex"
    )


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_canary_timeout_names_the_harness_and_the_seconds_waited(tmp_path, monkeypatch, target):
    report = tmp_path / "report.json"
    binding.request(report, "engineer", target)
    clock = iter([0, 91])
    monkeypatch.setattr(binding.time, "monotonic", lambda: next(clock))
    expected = (
        f"^mounted profile canary did not validate before timeout: the {target} agent reported no canary within 90s$"
    )
    with pytest.raises(ValueError, match=expected):
        binding.wait(report, 90.0)


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_deferred_canary_prints_the_pending_request_without_waiting(tmp_path, monkeypatch, capsys, target):
    assert _launch(tmp_path, monkeypatch, target, ["--defer-canary"]) == []
    lines = capsys.readouterr().out.splitlines()
    assert "profile_validation=pending" in lines
    (report,) = [line.removeprefix("profile_report=") for line in lines if line.startswith("profile_report=")]
    assert json.loads(Path(report).read_text()) == {"profile": "engineer", "harness": target, "state": "pending"}


def test_the_defer_canary_help_says_the_caller_confirms_later():
    (action,) = [a for a in init_agent._parser()._actions if "--defer-canary" in a.option_strings]
    assert action.help == (
        "Return once the session is routed and print its pending profile validation request for the caller "
        "to confirm later"
    )
    assert action.default is False


def test_a_deferred_canary_without_a_profile_request_still_waits():
    assert init_agent._binding_deferred({}) == []
    assert init_agent._binding_deferred({binding.REPORT: "/run/r.json"}) == [
        "profile_validation=pending",
        "profile_report=/run/r.json",
    ]


VALIDATED = {
    "state": "validated",
    "profile": "master",
    "harness": "claude",
    "pid": 77,
    "home": "/h",
    "persona": "p",
    "sources": "s",
    "account": "acct",
}


def _report(tmp_path, **data):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"profile": "master", "harness": "claude", **data}))
    return report


def test_confirm_reads_a_pending_request_as_not_yet_validated(tmp_path):
    assert binding.confirm(_report(tmp_path, state="pending"), "master", "claude", "acct") == {}


def test_confirm_raises_the_refusal_of_a_failed_canary(tmp_path):
    report = _report(tmp_path, state="failed", reason="mounted instruction canary mismatch")
    with pytest.raises(ValueError, match="^mounted instruction canary mismatch$"):
        binding.confirm(report, "master", "claude", "acct")


def test_confirm_returns_the_validated_binding(tmp_path):
    report = _report(tmp_path, state="validated", validation=VALIDATED)
    assert binding.confirm(report, "master", "claude", "acct") == VALIDATED
    assert binding.confirm(report, "master", "claude", "") == VALIDATED


def test_confirm_refuses_another_account(tmp_path):
    report = _report(tmp_path, state="validated", validation=VALIDATED)
    with pytest.raises(ValueError, match="^live process account differs from requested route$"):
        binding.confirm(report, "master", "claude", "other")


@pytest.mark.parametrize(
    ("changed", "message"),
    [
        ({"profile": "engineer"}, "validated live profile differs from requested choice"),
        ({"harness": "codex"}, "validated live profile differs from requested choice"),
        ({"pid": None}, "validated profile lacks process or rendered source evidence"),
        ({"state": "failed"}, "mounted profile validation is missing or failed"),
    ],
)
def test_confirm_refuses_a_binding_that_differs_from_the_launch(tmp_path, changed, message):
    report = _report(tmp_path, state="validated", validation={**VALIDATED, **changed})
    with pytest.raises(ValueError, match=f"^{message}$"):
        binding.confirm(report, "master", "claude", "acct")
