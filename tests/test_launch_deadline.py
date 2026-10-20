import pytest

from scripts import init_agent
from scripts.profiles import binding
from scripts.swarm import runtime


def _codex_launch(tmp_path, monkeypatch):
    from scripts import select_profile

    home = tmp_path / "engineer" / "codex"
    home.mkdir(parents=True)
    now = [0.0]
    waits = []
    monkeypatch.setattr(
        select_profile,
        "prepare",
        lambda *a: ({"AGENTIHOOKS_PROFILE": "engineer", binding.HOMES["codex"]: str(home)}, a[4]),
    )
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: ("codex", "explicit"))
    monkeypatch.setattr(init_agent.herdr_host, "rename_agent", lambda *a: True)
    monkeypatch.setattr(init_agent.herdr_host, "name_session", lambda *a: "named")
    monkeypatch.setattr(init_agent.herdr_host, "busy", lambda *a: "")
    monkeypatch.setattr(init_agent.herdr_host, "prompt", lambda *a: True)
    monkeypatch.setattr(init_agent.herdr_panes, "mark", lambda *a, **k: None)
    request = binding.request
    monkeypatch.setattr(binding, "request", lambda report, profile, agent, home: request(report, profile, agent))

    def launch(launcher, directory, name, args, agent, environ):
        init_agent._started_marker(launcher).touch()
        init_agent._route_report(launcher).write_text("status=routed\naccount=routed-account\n")
        return ["pane_id=w1:p7"]

    def ready(pane, environ, timeout):
        waits.append(("ready", timeout))
        now[0] += timeout
        return True

    def canary(report, timeout):
        waits.append(("canary", timeout))
        now[0] += timeout
        raise ValueError(f"the codex agent reported no canary within {timeout:g}s")

    monkeypatch.setattr(init_agent, "_start_herdr", launch)
    monkeypatch.setattr(init_agent.herdr_host, "wait_ready", ready)
    monkeypatch.setattr(binding, "wait", canary)
    monkeypatch.setattr(init_agent.time, "monotonic", lambda: now[0])
    argv = ["--host", "herdr", "--agent", "codex", "--profile", "engineer", "--dir", str(tmp_path)]
    result = init_agent.main(
        [*argv, "--route-timeout", "30", "--prompt", "Start."], {"XDG_RUNTIME_DIR": str(tmp_path / "rt")}
    )
    return result, now[0], waits


def test_a_codex_launch_with_both_ready_waits_at_their_limit_fails_inside_the_spawn_timeout_naming_the_wait(
    tmp_path, monkeypatch, capsys
):
    result, elapsed, waits = _codex_launch(tmp_path, monkeypatch)
    assert result == 3
    assert waits == [("ready", 120.0), ("ready", 120.0), ("canary", 30.0)]
    assert elapsed <= runtime.SPAWN_TIMEOUT_S
    assert capsys.readouterr().err == (
        "agentihooks init-agent: the 270s launch deadline ran out in the profile canary wait after "
        "Codex ready 120s, Codex ready for the opening prompt 120s, profile canary 30s: "
        "the codex agent reported no canary within 30s\n"
    )


def _clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(init_agent.time, "monotonic", lambda: now[0])
    return now


def _spend(now, seconds, result=True):
    def waiter(granted):
        now[0] += seconds
        return (granted, result)

    return waiter


def test_a_wait_after_the_deadline_is_spent_gets_no_time_and_keeps_the_first_wait_cut(monkeypatch):
    now = _clock(monkeypatch)
    deadline = init_agent.LaunchDeadline(100.0)
    assert deadline.run("first", 60.0, _spend(now, 60.0)) == (60.0, True)
    assert deadline.cut == ""
    assert deadline.run("second", 60.0, _spend(now, 50.0)) == (40.0, True)
    assert deadline.run("third", 60.0, _spend(now, 0.0)) == (0.0, True)
    assert deadline.cut == "second"
    assert deadline.spent == {"first": 60.0, "second": 50.0, "third": 0.0}


def test_a_wait_failing_inside_its_own_limit_raises_its_own_error(monkeypatch):
    _clock(monkeypatch)
    deadline = init_agent.LaunchDeadline(100.0)

    def fail(granted):
        raise ValueError(f"failed after {granted:g}s")

    with pytest.raises(ValueError, match="^failed after 30s$"):
        deadline.run("canary", 30.0, fail)
    assert deadline.spent == {"canary": 0.0}


def test_a_wait_repeated_by_name_adds_up_its_time(monkeypatch):
    now = _clock(monkeypatch)
    deadline = init_agent.LaunchDeadline(10.0)
    deadline.run("ready", 4.0, _spend(now, 4.0))

    def fail(granted):
        now[0] += granted
        raise ValueError("late")

    with pytest.raises(ValueError, match="^the 10s launch deadline ran out in the ready wait after ready 10s: late$"):
        deadline.run("ready", 8.0, fail)


def test_a_launch_without_a_deadline_never_cuts_a_wait(monkeypatch):
    _clock(monkeypatch)
    launch = init_agent.Launch(5.0)
    assert launch.deadline.run("ready", 120.0, lambda granted: granted) == 120.0
    assert launch.since == 5.0
    assert launch.deadline.cut == ""


def test_the_launch_deadline_leaves_the_session_rename_inside_the_spawn_timeout():
    assert init_agent.LAUNCH_DEADLINE_S == 270.0
    assert init_agent.LAUNCH_DEADLINE_S + init_agent.herdr_host.RENAME_CONFIRM_S < runtime.SPAWN_TIMEOUT_S
