from types import SimpleNamespace

import pytest

from scripts import codex_router
from scripts.codex_session import serve, supervisor
from scripts.routing import codex_api
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.unit

SWARM_ENV = {
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_SWARM_LANE": "eng",
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_AGENT_NAME": "engineer@abc-0001",
}


def test_take_route_reads_both_spellings_and_keeps_the_rest():
    assert serve.take_route(["-m", "m", "--route", "acct", "-c", "x=1"]) == ("acct", ["-m", "m", "-c", "x=1"])
    assert serve.take_route(["--route=interactive"]) == ("interactive", [])
    assert serve.take_route(["--route=a=b", "x"]) == ("a=b", ["x"])
    assert serve.take_route(["--route"]) == ("", ["--route"])
    assert serve.take_route(["x", "--route"]) == ("", ["x", "--route"])


def test_server_options_turn_the_model_into_config_and_keep_config_flags():
    model, effort, args = serve.server_options(
        ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', "--enable", "hooks", "-c", "x=1"]
    )
    assert (model, effort) == ("gpt-6.1-sol", "high")
    assert args == (
        "-c",
        'model="gpt-6.1-sol"',
        "-c",
        'model_reasoning_effort="high"',
        "--enable",
        "hooks",
        "-c",
        "x=1",
    )
    assert serve.server_options(["--model", "m"]) == ("m", "", ("-c", 'model="m"'))
    assert serve.server_options([]) == ("", "", ())


@pytest.mark.parametrize(
    ("flags", "reason"),
    [
        (["-s", "read-only"], "app-server takes only -m, -c, --enable and --disable; got -s"),
        (["--no-daemon", "x"], "got --no-daemon"),
        (["--config", "x=1"], "got --config"),
        (["-c"], "-c requires a value"),
    ],
)
def test_server_options_refuse_flags_the_app_server_lacks(flags, reason):
    with pytest.raises(ValueError, match=reason):
        serve.server_options(flags)


def test_provider_args_follow_the_routed_account():
    api = codex_router.CodexAccount("api", key_env="OPENAI_API_KEY", base_url="https://api.example.test/v1")
    assert serve.provider_args(api) == tuple(codex_api.overrides("OPENAI_API_KEY", "https://api.example.test/v1"))
    assert serve.provider_args(codex_router.CodexAccount("t", "AH_CX_TOKEN_t", bearer=True)) == tuple(
        codex_router.bearer_overrides()
    )
    assert serve.provider_args(codex_router.CodexAccount("default")) == ()


def test_account_type_is_checked_only_for_default_interactive_and_api_accounts():
    assert serve.account_type(codex_router.CodexAccount("api", key_env="OPENAI_API_KEY"), "") == "apiKey"
    assert serve.account_type(codex_router.CodexAccount("default"), "") == "chatgpt"
    assert serve.account_type(codex_router.CodexAccount("me"), serve.INTERACTIVE) == "chatgpt"
    assert serve.account_type(codex_router.CodexAccount("t", "AH_CX_TOKEN_t"), "") == ""
    assert serve.account_type(codex_router.CodexAccount("b", "AH_CX_TOKEN_b", bearer=True), "") == ""


def test_serve_refuses_outside_a_swarm_execution_and_reports_the_failure(tmp_path, capsys):
    report = tmp_path / "route"
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw", "--report", str(report)]
    assert serve.main(argv, {"HOME": str(tmp_path)}) == 2
    reason = f"the app-server transport runs only inside a swarm execution; missing {list(serve.IDENTITY_ENV)}"
    assert capsys.readouterr().err == f"agentihooks codex-session: {reason}\n"
    assert report.read_text() == f"status=failed\nerror={reason}\n"


def test_the_serve_parser_reads_every_option_and_its_defaults(capsys):
    parser = serve._parser()
    assert parser.prog == "python -m scripts.codex_session"
    required = ["--cwd", "/repo", "--profile", "engineer", "--seat", "eng-1@sw"]
    assert vars(parser.parse_args(["serve", *required])) == {
        "command": "serve",
        "cwd": "/repo",
        "profile": "engineer",
        "seat": "eng-1@sw",
        "overlay": [],
        "bundle_revision": "",
        "home": "",
        "exclude": "",
        "report": "",
        "previous_execution": "",
        "execution": "",
        "resume": "",
        "prompt_file": "",
        "startup_timeout": supervisor.STARTUP_TIMEOUT_S,
    }
    full = parser.parse_args(["serve", *required, "--overlay", "a", "--overlay", "b", "--startup-timeout", "7"])
    assert (full.overlay, full.startup_timeout) == (["a", "b"], 7.0)
    assert isinstance(full.startup_timeout, float)
    refused = [["start", *required], *(["serve", *required[:at], *required[at + 2 :]] for at in (0, 2, 4))]
    for argv in refused:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    capsys.readouterr()


def test_route_account_takes_the_interactive_login(monkeypatch):
    from scripts.routing import interactive

    seen = {}

    def account(agent, environ, runner):
        seen["account"] = (agent, environ, runner)
        return SimpleNamespace(slug="me")

    def child(environ, name):
        seen["child"] = (environ, name)
        return {"CHILD": "1"}

    monkeypatch.setattr(interactive, "account", account)
    monkeypatch.setattr(serve.envs, "codex_interactive_child", child)
    environ = {"HOME": "/h"}
    routed = serve.route_account(environ, serve.INTERACTIVE, "old")
    assert routed == (codex_router.CodexAccount("me"), "forced", {"CHILD": "1"})
    assert seen == {"account": ("codex", environ, serve.subprocess.run), "child": (environ, "me")}


def test_route_account_routes_then_builds_the_credentialed_child_environment(monkeypatch):
    routed = codex_router.CodexAccount("t", "AH_CX_TOKEN_t")
    credentialed = codex_router.CodexAccount("t", "AH_CX_TOKEN_t", bearer=True)
    seen = {}

    def route(environ, route, runner, exclude):
        seen["route"] = (environ, route, runner, exclude)
        return routed, "open", 1, "?"

    def credential(account, environ):
        seen["credential"] = (account, environ)
        return credentialed

    def child(account, environ):
        seen["child"] = (account, environ)
        return {"CHILD": "1"}

    monkeypatch.setattr(codex_router, "_route", route)
    monkeypatch.setattr(codex_router, "credential", credential)
    monkeypatch.setattr(codex_router, "child_environment", child)
    environ = {"HOME": "/h"}
    assert serve.route_account(environ, "t", "old") == (credentialed, "open", {"CHILD": "1"})
    assert seen == {
        "route": (environ, "t", serve.subprocess.run, "old"),
        "credential": (routed, environ),
        "child": (credentialed, environ),
    }


def test_prepare_routes_renders_and_builds_the_launch(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    prompt = tmp_path / "opening"
    prompt.write_text("Begin.", encoding="utf-8")
    calls = {}

    def prepare(name, agent, model, effort, args, environ, overlays):
        calls["prepare"] = (name, agent, model, effort, args, list(overlays), environ["AGENTIHOOKS_BUNDLE_REVISION"])
        calls["push"] = environ["AGENTIHOOKS_CODEX_PUSH"]
        return {"CODEX_HOME": str(home)}, ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', *args]

    def route(environ, route, exclude):
        calls["route"] = (route, exclude, environ["CODEX_HOME"], "AGENTIHOOKS_CODEX_PUSH" in environ)
        return codex_router.CodexAccount("t", "AH_CX_TOKEN_t"), "open", {"CODEX_HOME": str(home), "AH_CX_TOKEN_t": "x"}

    monkeypatch.setattr(serve.select_profile, "prepare", prepare)
    monkeypatch.setattr(serve, "route_account", route)
    monkeypatch.setattr(serve.time, "time", lambda: 1.5)
    argv = [
        "serve",
        "--cwd",
        str(tmp_path),
        "--profile",
        "engineer",
        "--seat",
        "eng-1@sw",
        "--overlay=frontend",
        "--bundle-revision=abc",
        "--exclude",
        "old",
        "--report",
        str(tmp_path / "route"),
        "--previous-execution",
        "exe-prev",
        "--execution",
        "exe-admitted",
        "--resume",
        "thread-kept",
        "--prompt-file",
        str(prompt),
        "--startup-timeout",
        "7",
    ]
    args = serve._parser().parse_args(argv)
    spec = serve.prepare(args, ["--route", "t", "-c", "x=1"], dict(SWARM_ENV))
    assert calls["prepare"] == ("engineer", "codex", "", "", ["-c", "x=1"], ["frontend"], "abc")
    assert calls["push"] == "1"
    assert calls["route"] == ("t", "old", str(home), False)
    assert (tmp_path / "route").read_text() == "status=routed\naccount=t\nplacement=open\n"
    assert (spec.slug, spec.agent.name, spec.agent.lane, spec.agent.task, spec.agent.seat) == (
        "sw",
        "engineer@abc-0001",
        "eng",
        "t1",
        "eng-1@sw",
    )
    assert (spec.agent.account, spec.agent.profile, spec.agent.model, spec.agent.effort, spec.agent.overlays) == (
        "t",
        "engineer",
        "gpt-6.1-sol",
        "high",
        ["frontend"],
    )
    assert spec.agent.started_at == 1500
    assert spec.server_args == ("-c", 'model="gpt-6.1-sol"', "-c", 'model_reasoning_effort="high"', "-c", "x=1")
    assert (spec.codex_home, spec.cwd, spec.environ["AH_CX_TOKEN_t"]) == (str(home), str(tmp_path), "x")
    assert (spec.previous_execution_id, spec.resume_thread, spec.opening) == ("exe-prev", "thread-kept", "Begin.")
    assert spec.execution_id == "exe-admitted"
    assert (spec.account_type, spec.allowed_tokens, spec.bundle_revision) == ("", frozenset({"AH_CX_TOKEN_t"}), "abc")
    assert spec.startup_timeout_s == 7.0


def test_prepare_mounts_the_home_init_agent_rendered_after_the_bundle_moved(tmp_path, monkeypatch):
    home = tmp_path / "rendered" / "engineer" / "codex"
    root = tmp_path / "engineer"
    root.mkdir()
    (root / "profile.yml").write_text("model: gpt-6.1-sol\neffort: high\n")
    inspected = []

    def rerender(*args, **kwargs):
        raise AssertionError("serve rendered the profile again after the bundle checkout moved")

    monkeypatch.setattr(serve.select_profile.profiles, "_chain", lambda name: [(name, root)])
    monkeypatch.setattr(serve.select_profile.profiles, "render", rerender)
    monkeypatch.setattr(serve.select_profile.profiles, "channels", lambda name: "amygdala,brain")
    monkeypatch.setattr(serve.select_profile.plugins, "claude_only", lambda name: False)
    monkeypatch.setattr(serve.select_profile.binding, "inspect", lambda *seen: inspected.append(seen))
    monkeypatch.setattr(
        serve, "route_account", lambda environ, route, exclude: (codex_router.CodexAccount("t"), "open", {})
    )
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw", f"--home={home}"]
    spec = serve.prepare(serve._parser().parse_args(argv), [], {**SWARM_ENV, "HOME": str(tmp_path)})
    assert inspected == [(home, "engineer", "codex")]
    assert spec.codex_home == str(home)


def test_prepare_checks_the_login_of_an_interactive_route_and_opens_nothing_without_a_prompt(tmp_path, monkeypatch):
    def prepare(name, agent, model, effort, args, environ, overlays):
        return {"CODEX_HOME": str(tmp_path)}, list(args)

    def route(environ, route, exclude):
        return codex_router.CodexAccount("t", "AH_CX_TOKEN_t"), "forced", {}

    monkeypatch.setattr(serve.select_profile, "prepare", prepare)
    monkeypatch.setattr(serve, "route_account", route)
    args = serve._parser().parse_args(["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"])
    spec = serve.prepare(args, ["--route", serve.INTERACTIVE], dict(SWARM_ENV))
    assert (spec.account_type, spec.opening, spec.agent.effort) == ("chatgpt", "", "")


def _serve_with(tmp_path, monkeypatch, inherited):
    monkeypatch.setattr(serve.select_profile, "prepare", lambda *_: ({"CODEX_HOME": str(tmp_path)}, []))
    account = codex_router.CodexAccount("t", "AH_CX_TOKEN_t")
    monkeypatch.setattr(serve, "route_account", lambda environ, route, exclude: (account, "open", dict(environ)))
    args = serve._parser().parse_args(["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"])
    return serve.prepare(args, [], {**SWARM_ENV, **inherited})


def test_prepare_pins_the_agentihooks_langfuse_keys_over_inherited_ones(tmp_path, monkeypatch):
    home = tmp_path / "agentihooks"
    home.mkdir()
    (home / "langfuse.env").write_text("LANGFUSE_PUBLIC_KEY=public-swarm\nLANGFUSE_SECRET_KEY=secret-swarm\n")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    inherited = {
        "LANGFUSE_PUBLIC_KEY": "public-other",
        "LANGFUSE_SECRET_KEY": "secret-other",
        "OTEL_LANGFUSE_PUBLIC_KEY": "public-stale",
    }
    spec = _serve_with(tmp_path, monkeypatch, inherited)
    assert (spec.environ["OTEL_LANGFUSE_PUBLIC_KEY"], spec.environ["OTEL_LANGFUSE_SECRET_KEY"]) == (
        "public-swarm",
        "secret-swarm",
    )


def test_prepare_keeps_inherited_langfuse_keys_without_agentihooks_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "absent"))
    spec = _serve_with(tmp_path, monkeypatch, {"OTEL_LANGFUSE_PUBLIC_KEY": "public-own"})
    assert spec.environ["OTEL_LANGFUSE_PUBLIC_KEY"] == "public-own"
    assert "OTEL_LANGFUSE_SECRET_KEY" not in spec.environ


def test_the_opening_prompt_reads_as_utf8_text_with_every_line_ending_as_a_newline(tmp_path):
    prompt = tmp_path / "opening"
    prompt.write_bytes("café\r\nline\rlast\n".encode())
    assert serve._opening(str(prompt)) == "café\nline\nlast\n"
    assert serve._opening(str(prompt)) == prompt.read_text(encoding="utf-8")
    assert serve._opening("") == ""


def supervised(monkeypatch, wait, returncode=0):
    process = SimpleNamespace(wait=lambda timeout=None: wait(), returncode=returncode)
    pump = SimpleNamespace(poll=lambda: None, returncode=None)
    record = SimpleNamespace(
        execution_id="exe-1", thread_id="thread-1", endpoint_ref="unix:///run/cx/s", state="connected"
    )
    started = SimpleNamespace(record=record, process=process)
    spec = SimpleNamespace(slug="sw", cwd="/workspace", environ=dict(SWARM_ENV))
    seen = {
        "finished": [],
        "stopped": [],
        "signals": [],
        "prepared": [],
        "connected": [],
        "launched": [],
        "pump": pump,
        "spawns": [],
    }

    def prepare(args, agent_args, environ):
        seen["prepared"].append((args.cwd, agent_args, environ))
        return spec

    def connect(environ):
        seen["connected"].append(environ)
        return "store"

    def launch(store, given, deps):
        seen["launched"].append((store, given, deps))
        return started

    monkeypatch.setattr(
        serve.subprocess, "Popen", lambda command, **kwargs: seen["spawns"].append((command, kwargs)) or pump
    )
    monkeypatch.setattr(serve, "prepare", prepare)
    monkeypatch.setattr(serve, "connect", connect)
    monkeypatch.setattr(serve.signal, "signal", lambda signum, handler: seen["signals"].append((signum, handler)))
    monkeypatch.setattr(serve.time, "time", lambda: 1.5)
    monkeypatch.setattr(supervisor, "launch", launch)
    monkeypatch.setattr(supervisor, "stop", lambda proc: seen["stopped"].append(proc))
    monkeypatch.setattr(
        supervisor, "finish", lambda store, slug, done, now: seen["finished"].append((store, slug, done, now))
    )
    return started, seen


def test_main_supervises_the_server_until_it_exits(tmp_path, monkeypatch, capsys):
    started, seen = supervised(monkeypatch, lambda: 0)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw", "--", "-c", "x", "--", "y"]
    assert serve.main(argv, dict(SWARM_ENV)) == 0
    assert capsys.readouterr().out == (
        "[agentihooks codex-session] execution=exe-1 thread=thread-1 endpoint=unix:///run/cx/s state=connected\n"
    )
    assert seen["prepared"] == [(str(tmp_path), ["-c", "x", "--", "y"], dict(SWARM_ENV))]
    assert seen["connected"] == [dict(SWARM_ENV)]
    [(store, spec, deps)] = seen["launched"]
    assert (store, spec.slug, type(deps)) == ("store", "sw", supervisor.Deps)
    assert seen["finished"] == [("store", "sw", started, 1500)]
    assert seen["stopped"] == [seen["pump"], started.process]
    assert seen["signals"] == [
        (serve.signal.SIGTERM, serve._exit_on_signal),
        (serve.signal.SIGHUP, serve._exit_on_signal),
    ]


@pytest.mark.parametrize(("pump_code", "exit_code"), [(9, 9), (0, 3)])
def test_an_exited_pump_retires_the_server_and_finishes_the_execution(tmp_path, monkeypatch, pump_code, exit_code):
    started, seen = supervised(monkeypatch, lambda: pytest.fail("server must not wait without its pump"))
    seen["pump"].poll = lambda: pump_code
    seen["pump"].returncode = pump_code
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == exit_code
    assert seen["stopped"] == [seen["pump"], started.process]
    assert seen["finished"] == [("store", "sw", started, 1500)]


def test_an_exited_pump_names_its_status_before_the_server_stops(tmp_path, monkeypatch, capsys):
    started, seen = supervised(monkeypatch, lambda: pytest.fail("server must not wait without its pump"))
    seen["pump"].poll = lambda: 1
    seen["pump"].returncode = 1
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == 1
    assert capsys.readouterr().err == serve.PUMP_EXITED.format(1) + "\n"
    assert seen["stopped"] == [seen["pump"], started.process]


def test_a_failed_pump_spawn_retires_the_server_and_finishes_the_execution(tmp_path, monkeypatch):
    started, seen = supervised(monkeypatch, lambda: pytest.fail("server must not wait without its pump"))

    def refuse(*args, **kwargs):
        raise OSError("pump could not start")

    monkeypatch.setattr(serve.subprocess, "Popen", refuse)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    with pytest.raises(OSError, match="pump could not start"):
        serve.main(argv, dict(SWARM_ENV))
    assert seen["stopped"] == [started.process]
    assert seen["finished"] == [("store", "sw", started, 1500)]


def test_a_pump_dying_while_the_server_is_busy_ends_supervision(tmp_path, monkeypatch):
    started, seen = supervised(monkeypatch, lambda: None)

    def busy(timeout):
        assert timeout == supervisor.POLL_S
        seen["pump"].poll = lambda: 7
        seen["pump"].returncode = 7
        raise serve.subprocess.TimeoutExpired("server", timeout)

    started.process.wait = busy
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == 7
    assert seen["stopped"] == [seen["pump"], started.process]
    assert seen["finished"] == [("store", "sw", started, 1500)]


def test_main_reads_the_process_arguments_and_environment_and_keeps_given_deps(tmp_path, monkeypatch):
    _, seen = supervised(monkeypatch, lambda: 0, returncode=5)
    for name, value in SWARM_ENV.items():
        monkeypatch.setenv(name, value)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    monkeypatch.setattr(serve.sys, "argv", ["codex_session", *argv])
    given = supervisor.Deps()
    assert serve.main(deps=given) == 5
    assert seen["prepared"] == [(str(tmp_path), [], dict(serve.os.environ))]
    assert seen["connected"] == [dict(serve.os.environ)]
    assert seen["launched"][0][2] is given


def test_main_flushes_the_started_line_before_it_waits(tmp_path, monkeypatch):
    class Stdout:
        def __init__(self):
            self.text = []
            self.flushes = 0

        def write(self, text):
            self.text.append(text)

        def flush(self):
            self.flushes += 1

    out = Stdout()
    flushed_before_wait = []
    supervised(monkeypatch, lambda: flushed_before_wait.append(out.flushes))
    monkeypatch.setattr(serve.sys, "stdout", out)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == 0
    assert flushed_before_wait == [1]


def test_a_signal_while_supervising_still_stops_the_server_and_finishes_the_record(tmp_path, monkeypatch):
    def interrupted():
        serve._exit_on_signal(serve.signal.SIGTERM, None)

    started, seen = supervised(monkeypatch, interrupted)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    with pytest.raises(SystemExit) as stopped:
        serve.main(argv, dict(SWARM_ENV))
    assert stopped.value.code == 128 + serve.signal.SIGTERM
    assert seen["stopped"] == [seen["pump"], started.process]
    assert seen["finished"] == [("store", "sw", started, 1500)]


def test_the_signal_handlers_are_in_place_before_the_launch_starts(tmp_path, monkeypatch):
    _, seen = supervised(monkeypatch, lambda: 0)
    installed_at_launch = []

    def launching(store, spec, deps):
        installed_at_launch.extend(seen["signals"])
        serve._exit_on_signal(serve.signal.SIGHUP, None)

    monkeypatch.setattr(supervisor, "launch", launching)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    with pytest.raises(SystemExit) as stopped:
        serve.main(argv, dict(SWARM_ENV))
    assert stopped.value.code == 128 + serve.signal.SIGHUP
    assert [signum for signum, _ in installed_at_launch] == [serve.signal.SIGTERM, serve.signal.SIGHUP]
    assert seen["finished"] == []


def test_main_reports_a_refused_launch(tmp_path, monkeypatch, capsys):
    def refused(store, spec, deps):
        raise supervisor.LaunchFailure("hooks_missing", "required hooks are missing")

    monkeypatch.setattr(serve, "prepare", lambda args, agent_args, environ: SimpleNamespace(slug="sw", environ={}))
    monkeypatch.setattr(serve, "connect", lambda environ: "store")
    monkeypatch.setattr(supervisor, "launch", refused)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == 3
    assert "launch refused: required hooks are missing" in capsys.readouterr().err


def test_a_refused_launch_fails_the_profile_report_so_init_agent_reports_it_at_once(tmp_path, monkeypatch, capsys):
    from scripts import init_agent
    from scripts.profiles import binding

    report = tmp_path / "profile.json"
    binding.request(report, "engineer", "codex")

    def refused(store, spec, deps):
        raise SwarmError("replacement does not match the current execution")

    spec = SimpleNamespace(slug="sw", environ={binding.REPORT: str(report)})
    monkeypatch.setattr(serve, "prepare", lambda args, agent_args, environ: spec)
    monkeypatch.setattr(serve, "connect", lambda environ: "store")
    monkeypatch.setattr(supervisor, "launch", refused)
    argv = ["serve", "--cwd", str(tmp_path), "--profile", "engineer", "--seat", "eng-1@sw"]
    assert serve.main(argv, dict(SWARM_ENV)) == 3
    capsys.readouterr()
    monkeypatch.setattr(binding.time, "sleep", lambda seconds: pytest.fail("init-agent waited on a refused launch"))
    env = {binding.REPORT: str(report)}
    assert init_agent._selection_refused(env)
    assert init_agent._route_reported(env, 30.0, {}) is None
    with pytest.raises(ValueError) as failure:
        init_agent._binding_result(env, 90.0, {})
    assert str(failure.value) == "codex-session launch refused: replacement does not match the current execution"
