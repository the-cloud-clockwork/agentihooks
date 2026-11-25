import subprocess

import pytest

from hooks.lifecycle import herdr_resume

SESSION = "97f04df2-8214-4ef0-943b-ff8bd10254ca"
PANE = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w6:p5", "HERDR_BIN_PATH": "/opt/herdr"}
ROUTED = {**PANE, "AH_CC_TOKEN_work": "token-value"}


class Recorder:
    def __init__(self, returncode=0, stdout="", raises=None):
        self.calls = []
        self.returncode, self.stdout, self.raises = returncode, stdout, raises

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "")


@pytest.mark.feature("herdr-native-resume")
def test_a_routed_claude_pane_resumes_through_the_router():
    assert herdr_resume.resume_argv(ROUTED, SESSION, "claude") == [
        "agentihooks",
        "claude",
        "--agentihooks-fallback-bare",
        "--resume",
        SESSION,
    ]


@pytest.mark.feature("herdr-native-resume")
def test_a_routed_pane_keeps_its_profile_home():
    environ = {**ROUTED, "CLAUDE_CONFIG_DIR": "/home/u/.agentihooks/profiles/smith/claude"}
    assert (herdr_resume.resume_argv(environ, SESSION, "claude") or [])[:3] == [
        "env",
        "CLAUDE_CONFIG_DIR=/home/u/.agentihooks/profiles/smith/claude",
        "agentihooks",
    ]


@pytest.mark.feature("herdr-native-resume")
def test_a_routed_codex_pane_resumes_through_the_router():
    environ = {**PANE, "AH_CX_TOKEN_work": "token-value", "CODEX_HOME": "/home/u/.codex"}
    assert herdr_resume.resume_argv(environ, SESSION, "codex") == [
        "env",
        "CODEX_HOME=/home/u/.codex",
        "agentihooks",
        "codex",
        "resume",
        SESSION,
    ]


@pytest.mark.feature("herdr-native-resume")
@pytest.mark.parametrize(
    "environ, target",
    [
        (PANE, "claude"),
        ({**PANE, "AH_CC_TOKEN_a": "x", "AH_CC_TOKEN_b": "y"}, "claude"),
        (PANE, "codex"),
        ({**ROUTED, "AGENTIHOOKS_SWARM": "rig"}, "claude"),
        ({**ROUTED, "AGENTIHOOKS_HERDR_RESUME": "off"}, "claude"),
        ({**ROUTED, "HERDR_ENV": ""}, "claude"),
        ({key: value for key, value in ROUTED.items() if key != "HERDR_PANE_ID"}, "claude"),
        ({key: value for key, value in ROUTED.items() if key != "HERDR_BIN_PATH"}, "claude"),
        (ROUTED, "copilot"),
        ({**ROUTED, "CLAUDE_CONFIG_DIR": "/home/o'neil/.claude"}, "claude"),
    ],
)
def test_herdr_keeps_its_own_resume_when_agentihooks_has_nothing_to_add(environ, target):
    assert herdr_resume.resume_argv(environ, SESSION, target) is None


@pytest.mark.feature("herdr-native-resume")
@pytest.mark.parametrize("marker", ["AH_ROUTE_API", "AH_ROUTE_INTERACTIVE_work"])
def test_a_session_the_router_sent_to_the_api_or_a_login_resumes_through_it(marker):
    assert (herdr_resume.resume_argv({**PANE, marker: "1"}, SESSION, "claude") or [""])[0] == "agentihooks"


@pytest.mark.feature("herdr-native-resume")
def test_an_empty_session_id_reports_nothing():
    assert herdr_resume.resume_argv(ROUTED, "", "claude") is None


@pytest.mark.feature("herdr-native-resume")
def test_the_report_hands_herdr_the_routed_command_for_this_pane():
    run = Recorder()
    assert herdr_resume.report({"session_id": SESSION}, ROUTED, run) is True
    ((command, kwargs),) = run.calls
    assert command[:5] == ["/opt/herdr", "pane", "report-agent-session", "w6:p5", "--source"]
    assert command[5:8] == ["agentihooks", "--agent", "claude"]
    assert command[8] == "--seq" and command[9].isdigit()
    assert command[10:] == ["--", "agentihooks", "claude", "--agentihooks-fallback-bare", "--resume", SESSION]
    assert kwargs == {"capture_output": True, "text": True, "timeout": herdr_resume.TIMEOUT_S}


@pytest.mark.feature("herdr-native-resume")
def test_a_codex_session_reports_as_codex():
    run = Recorder()
    environ = {**PANE, "AH_CX_TOKEN_work": "x", "AGENTIHOOKS_TARGET": "codex"}
    assert herdr_resume.report({"session_id": SESSION}, environ, run) is True
    assert run.calls[0][0][7] == "codex"
    assert run.calls[0][0][-4:] == ["agentihooks", "codex", "resume", SESSION]


@pytest.mark.feature("herdr-native-resume")
def test_an_unrouted_session_never_calls_herdr():
    run = Recorder()
    assert herdr_resume.report({"session_id": SESSION}, PANE, run) is False
    assert run.calls == []


@pytest.mark.feature("herdr-native-resume")
@pytest.mark.parametrize(
    "run, error",
    [
        (
            Recorder(returncode=1, stdout=' {"error":{"code":"resume_not_accepted"}} '),
            '{"error":{"code":"resume_not_accepted"}}',
        ),
        (Recorder(returncode=1, stdout="x" * 300), "x" * 200),
        (Recorder(raises=OSError("gone")), "OSError"),
        (Recorder(raises=subprocess.TimeoutExpired("herdr", herdr_resume.TIMEOUT_S)), "TimeoutExpired"),
    ],
)
def test_a_refused_or_failed_report_is_logged_and_dropped(monkeypatch, run, error):
    logged = []
    monkeypatch.setattr(herdr_resume, "log", lambda message, data: logged.append((message, data)))
    assert herdr_resume.report({"session_id": SESSION}, ROUTED, run) is False
    assert logged == [(herdr_resume.FAILED, {"error": error})]


@pytest.mark.feature("herdr-native-resume")
def test_failures_reach_the_agentihooks_log():
    from hooks import common

    assert herdr_resume.log is common.log


@pytest.mark.feature("herdr-native-resume")
def test_the_kick_detaches_the_report(monkeypatch):
    jobs = []
    monkeypatch.setattr("hooks._async.fork_and_call", lambda func, *args, **kwargs: jobs.append((func, args, kwargs)))
    assert herdr_resume.kick({"session_id": SESSION}, ROUTED) is True
    ((func, args, kwargs),) = jobs
    assert func is herdr_resume.report
    assert args == ({"session_id": SESSION}, ROUTED)
    assert kwargs == {"timeout_sec": 10, "task_name": "herdr-resume-report"}


@pytest.mark.feature("herdr-native-resume")
def test_the_kick_forks_nothing_when_herdr_keeps_its_own_resume(monkeypatch):
    jobs = []
    monkeypatch.setattr("hooks._async.fork_and_call", lambda *args, **kwargs: jobs.append(args))
    assert herdr_resume.kick({"session_id": SESSION}, PANE) is False
    assert jobs == []
