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
