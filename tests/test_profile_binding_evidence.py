import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.profiles import binding, render


@pytest.fixture
def mounted(tmp_path, monkeypatch):
    home = tmp_path / "engineer" / "claude"
    home.mkdir(parents=True)
    (home / "CLAUDE.md").write_text(binding.persona("Engineer instructions.\n"))
    rows = [{"locator": {"repo": "/fixture/source", "path": "persona.md", "blob": "source-blob"}}]
    (home.parent / "claude.sources.json").write_text(json.dumps(rows))
    calls = []

    def revision(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="source-revision\n")

    monkeypatch.setattr(binding.subprocess, "run", revision)
    binding.write(home, "engineer", "claude")
    report = tmp_path / "report.json"
    binding.request(report, "engineer", "claude")
    env = {
        "AGENTIHOOKS_PROFILE": "engineer",
        "CLAUDE_CONFIG_DIR": str(home),
        binding.REPORT: str(report),
        "AGENTIHOOKS_RUN_MODEL": "opus",
        "AGENTIHOOKS_RUN_EFFORT": "medium",
    }
    monkeypatch.setattr(binding, "process", lambda: (123, "claude", env, "account-one"))
    monkeypatch.setattr(binding.time, "time", lambda: 123.25)
    return home, report, env, calls, rows


def test_canary_exports_complete_process_and_rendered_source_evidence(mounted, capsys):
    home, report, env, calls, rows = mounted
    assert binding.main("8feea91e5da2548a6d9c82e0") == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result) == {
        "state",
        "pid",
        "account",
        "validated_at",
        "model",
        "effort",
        "profile",
        "harness",
        "home",
        "persona",
        "canary",
        "sources",
        "revisions",
        "source_blobs",
    }
    assert {key: result[key] for key in ("state", "pid", "account", "validated_at", "model", "effort")} == {
        "state": "validated",
        "pid": 123,
        "account": "account-one",
        "validated_at": 123.25,
        "model": "opus",
        "effort": "medium",
    }
    assert (result["profile"], result["harness"], result["home"], result["canary"]) == (
        "engineer",
        "claude",
        str(home),
        "8feea91e5da2548a6d9c82e0",
    )
    assert re.fullmatch("[a-f0-9]{64}", result["persona"])
    assert re.fullmatch("[a-f0-9]{64}", result["sources"])
    assert result["source_blobs"] == [rows[0]["locator"]]
    assert result["revisions"] == {"/fixture/source": "source-revision"}
    assert calls == [(["git", "-C", "/fixture/source", "rev-parse", "HEAD"], {"capture_output": True, "text": True})]
    assert json.loads(report.read_text()) == {
        "profile": "engineer",
        "harness": "claude",
        "state": "validated",
        "validation": result,
    }
    assert report.read_text().endswith("\n")
    assert report.stat().st_mode & 0o777 == 0o600
    assert binding.wait(report, 1) == result
    wire = {"profile_validation": "validated", "profile_binding": json.dumps(result)}
    assert binding.fields(wire, "engineer", "claude") == result


@pytest.mark.parametrize("key", ["profile", "harness", "home"])
def test_rendered_identity_mismatch_is_refused(mounted, key):
    home, *_ = mounted
    file = home / binding.FILE
    data = json.loads(file.read_text())
    data[key] = "wrong"
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="^profile mismatch in rendered binding$"):
        binding.inspect(home, "engineer", "claude")


def test_changed_source_manifest_is_refused(mounted):
    home, *_ = mounted
    (home.parent / "claude.sources.json").write_text("[]")
    with pytest.raises(ValueError, match="^rendered source manifest changed$"):
        binding.inspect(home, "engineer", "claude")


def test_unset_home_and_wrong_harness_cannot_claim_validation(mounted, monkeypatch, capsys):
    home, report, env, *_ = mounted
    env.pop("CLAUDE_CONFIG_DIR")
    assert binding.main("8feea91e5da2548a6d9c82e0") == 2
    assert (
        capsys.readouterr().err
        == "profile validation failed: missing profile home: CLAUDE_CONFIG_DIR is unset; read the binding record with agentihooks profile binding\n"
    )
    assert json.loads(report.read_text())["state"] == "failed"
    monkeypatch.setattr(binding, "process", lambda: (123, "codex", env, "account-one"))
    assert binding.main("8feea91e5da2548a6d9c82e0") == 2
    assert (
        capsys.readouterr().err
        == "profile validation failed: live harness profile mismatch with requested choice; read the binding record with agentihooks profile binding\n"
    )


def test_missing_launch_request_and_wrong_canary_report_failure(mounted, capsys):
    _, report, env, *_ = mounted
    assert binding.main("wrong") == 2
    assert (
        capsys.readouterr().err
        == "profile validation failed: mounted instruction canary mismatch; read the binding record with agentihooks profile binding\n"
    )
    assert json.loads(report.read_text())["reason"] == "mounted instruction canary mismatch"
    env.pop(binding.REPORT)
    assert binding.main("wrong") == 2
    assert (
        capsys.readouterr().err
        == "profile validation failed: profile canary has no launch validation request; read the binding record with agentihooks profile binding\n"
    )


def test_wait_observes_failure_and_times_out_on_pending_request(mounted):
    _, report, *_ = mounted
    binding._report(report, {"state": "failed", "reason": "wrong profile"})
    with pytest.raises(ValueError, match="^wrong profile$"):
        binding.wait(report, 1)
    binding.request(report, "engineer", "claude")
    assert json.loads(report.read_text()) == {"profile": "engineer", "harness": "claude", "state": "pending"}
    with pytest.raises(ValueError, match="^mounted profile canary did not validate before timeout$"):
        binding.wait(report, 0)


def test_malformed_or_missing_wire_evidence_is_refused(mounted):
    home, *_ = mounted
    result = {**binding.inspect(home, "engineer", "claude"), "state": "validated", "pid": 123}
    for key in ("pid", "home", "persona", "sources"):
        missing = {k: v for k, v in result.items() if k != key}
        with pytest.raises(ValueError, match="^validated profile lacks process or rendered source evidence$"):
            binding.fields(
                {"profile_validation": "validated", "profile_binding": json.dumps(missing)}, "engineer", "claude"
            )
    for wire in ({}, {"profile_validation": "validated", "profile_binding": "[]"}):
        with pytest.raises(ValueError, match="^mounted profile validation is missing or failed$"):
            binding.fields(wire, "engineer", "claude")
    for profile, harness in (("qa", "claude"), ("engineer", "codex")):
        with pytest.raises(ValueError, match="^validated live profile differs from requested choice$"):
            binding.fields({"profile_validation": "validated", "profile_binding": json.dumps(result)}, profile, harness)


def test_mounted_command_uses_the_renderer_source_and_separate_canary_argument():
    text = binding.persona("Engineer instructions.\n")
    command = re.search(r"`([^`]+)`", text).group(1)
    args = shlex.split(command)
    assert args[-2:] == ["--canary", "8feea91e5da2548a6d9c82e0"]
    assert args[1] == "-c"
    assert str(Path(binding.__file__).resolve().parents[2]) in args[2]
    assert "from scripts.profiles.binding import main" in args[2]
    assert "main(sys.argv[2])" in args[2]
    assert "execute" in text and "through your shell tool" in text
    assert "the opening prompt does not supply it" in text


def test_validate_command_requires_canary_and_reports_actual_result(mounted, capsys):
    assert render.main(["validate", "--canary", "8feea91e5da2548a6d9c82e0"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "validated"
    with pytest.raises(SystemExit) as error:
        render.main(["validate"])
    assert error.value.code == 2


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_native_process_falls_back_to_preserved_options_and_reads_the_callers_parent(tmp_path, monkeypatch, target):
    shell, agent = tmp_path / "3", tmp_path / "2"
    shell.mkdir()
    agent.mkdir()
    (shell / "comm").write_text("bash\n")
    (shell / "status").write_text("PPid: 2\n")
    (agent / "comm").write_text(target + "\n")
    fields = [
        "AH_CC_TOKEN_fixtureclaude",
        "AH_CX_TOKEN_fixturecodex",
        "AGENTIHOOKS_PROFILE=engineer",
        f"{binding.HOMES[target]}=/profile/home",
        "AGENTIHOOKS_RUN_MODEL=preserved-model",
        "AGENTIHOOKS_RUN_EFFORT=medium",
        "AGENTIHOOKS_PROFILE_REPORT=/request/report",
        "EXTRA_DATA=omitted",
    ]
    (agent / "environ").write_bytes("\0".join(fields).encode())
    (agent / "cmdline").write_bytes(target.encode())
    monkeypatch.setattr(binding.os, "getppid", lambda: 3)
    pid, actual, env, account = binding.process(tmp_path)
    assert (pid, actual, account) == (2, target, "fixture" + target)
    assert env == {
        "AGENTIHOOKS_PROFILE": "engineer",
        binding.HOMES[target]: "/profile/home",
        "AGENTIHOOKS_RUN_MODEL": "preserved-model",
        "AGENTIHOOKS_RUN_EFFORT": "medium",
        "AGENTIHOOKS_PROFILE_REPORT": "/request/report",
    }


@pytest.mark.parametrize(
    "environ",
    [
        {"AGENTIHOOKS_SWARM_EFFORT_RANGE": "high:high", "AGENTIHOOKS_SWARM_LANE": "eng"},
        {},
    ],
)
def test_quota_continuation_preserves_options_or_refuses_policy_incompatibility(monkeypatch, environ):
    env = {"AGENTIHOOKS_RUN_MODEL": "opus", "AGENTIHOOKS_RUN_EFFORT": "medium"}
    monkeypatch.setattr(binding, "process", lambda: (123, "claude", env, "one"))
    if environ:
        with pytest.raises(ValueError, match="saved effort"):
            binding.continuation(["--route", "two"], "claude", environ)
    else:
        assert binding.continuation(["--route", "two"], "claude", environ) == [
            "--model",
            "opus",
            "--effort",
            "medium",
            "--route",
            "two",
        ]
    env.pop("AGENTIHOOKS_RUN_MODEL")
    with pytest.raises(ValueError, match="^unsupported quota transfer: original model and effort binding unavailable$"):
        binding.continuation([], "claude", {})


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_launcher_exports_the_binding_to_the_native_child(tmp_path, monkeypatch, target):
    import os
    import subprocess
    import sys

    from scripts import init_agent

    output = tmp_path / "observed.json"
    probe = tmp_path / "probe.py"
    keys = (
        "AGENTIHOOKS_PROFILE_REPORT",
        "AGENTIHOOKS_HOME",
        "AGENTIHOOKS_PROFILE",
        "CODEX_HOME",
        "AGENTIHOOKS_RUN_MODEL",
        "AGENTIHOOKS_RUN_EFFORT",
    )
    probe.write_text(
        f"import json, os\nfrom pathlib import Path\nPath({str(output)!r}).write_text(json.dumps({{key: os.environ.get(key) for key in {keys!r}}}))\n"
    )
    exit_shell = tmp_path / "exit-shell"
    exit_shell.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    exit_shell.chmod(0o700)
    env = {
        "XDG_RUNTIME_DIR": str(tmp_path),
        "SHELL": str(exit_shell),
        "AGENTIHOOKS_PROFILE_REPORT": "/fixture/request",
        "AGENTIHOOKS_HOME": "/fixture/install",
        "AGENTIHOOKS_PROFILE": "engineer",
        "CODEX_HOME": "/fixture/codex",
        "AGENTIHOOKS_RUN_MODEL": "preserved-model",
        "AGENTIHOOKS_RUN_EFFORT": "medium",
    }
    monkeypatch.setattr(init_agent, "_agent_command", lambda *a: ([sys.executable, str(probe)], ""))
    launcher, _ = init_agent._write_launcher(tmp_path, "probe", "", [], env, init_agent.AgentSpec(agent=target))
    run = subprocess.run(["bash", str(launcher)], env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(output.read_text()) == {key: env[key] for key in keys}


@pytest.mark.parametrize("args", [["--resume", "conversation"], ["--agent", "codex", "--", "resume", "conversation"]])
def test_native_resume_flags_need_the_original_profile(tmp_path, monkeypatch, capsys, args):
    from scripts import init_agent

    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, *a: (requested or "claude", "explicit"))
    assert init_agent.main(["--dir", str(tmp_path), *args], {}) == 2
    assert (
        capsys.readouterr().err
        == "agentihooks init-agent: unsupported continuation: original required profile is missing; pass --profile\n"
    )


def test_inherited_validation_report_cannot_validate_a_new_unprofiled_launch(tmp_path, monkeypatch, capsys):
    from scripts import init_agent

    inherited = tmp_path / "old-report.json"
    inherited.write_text('{"state":"validated","validation":{"profile":"qa"}}')
    observed = {}
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: ("codex", "explicit"))

    def launch(launcher, directory, name, args, agent, environ):
        observed.update(environ)
        init_agent._started_marker(launcher).touch()
        init_agent._route_report(launcher).write_text("status=direct\n")
        return []

    monkeypatch.setattr(init_agent, "_start_herdr", launch)
    assert (
        init_agent.main(
            ["--host", "herdr", "--dir", str(tmp_path)],
            {binding.REPORT: str(inherited), "XDG_RUNTIME_DIR": str(tmp_path)},
        )
        == 0
    )
    assert binding.REPORT not in observed
    assert "profile_binding=" not in capsys.readouterr().out


def test_field_validation_requires_both_producer_and_payload_success(mounted):
    home, *_ = mounted
    result = {**binding.inspect(home, "engineer", "claude"), "state": "validated", "pid": 123}
    for producer, payload in (("failed", "validated"), ("validated", "failed")):
        wire = {"profile_validation": producer, "profile_binding": json.dumps({**result, "state": payload})}
        with pytest.raises(ValueError, match="^mounted profile validation is missing or failed$"):
            binding.fields(wire, "engineer", "claude")


def test_invalid_harness_missing_evidence_and_persona_drift_have_reasons(mounted):
    home, *_ = mounted
    with pytest.raises(ValueError, match="^unsupported profile binding harness: copilot$"):
        binding.inspect(home, "engineer", "copilot")
    (home / "CLAUDE.md").write_text("changed")
    with pytest.raises(ValueError, match="^mounted persona changed since render$"):
        binding.inspect(home, "engineer", "claude")
    (home / binding.FILE).unlink()
    with pytest.raises(ValueError, match="^missing or invalid rendered profile evidence:"):
        binding.inspect(home, "engineer", "claude")


def test_process_decodes_malformed_names_and_values_without_exporting_unrelated_data(tmp_path, monkeypatch):
    from hooks.context import account_sessions

    agent = tmp_path / "2"
    agent.mkdir()
    (agent / "comm").write_text("claude\n")
    (agent / "environ").write_bytes(b"EXTRA_NAME=a=b\0bad\xffname=value\0CLAUDE_CONFIG_DIR=/home/\xff=data\0")
    (agent / "cmdline").write_bytes(b"claude\0ignored\xff\0")
    observed = []
    monkeypatch.setattr(account_sessions, "account_from_names", lambda names: observed.extend(names) or "selected")
    pid, target, env, account = binding.process(tmp_path, 2)
    assert observed == ["EXTRA_NAME", "bad\ufffdname", "CLAUDE_CONFIG_DIR", ""]
    assert (pid, target, account) == (2, "claude", "selected")
    assert env == {"CLAUDE_CONFIG_DIR": "/home/\ufffd=data", "AGENTIHOOKS_RUN_MODEL": "", "AGENTIHOOKS_RUN_EFFORT": ""}


def test_source_evidence_deduplicates_repositories_and_names_non_git_gaps(mounted, monkeypatch):
    home, _, _, _, _ = mounted
    rows = [
        {"locator": {"repo": "/fixture/source", "path": "one", "blob": "one"}},
        {"locator": {"repo": "/fixture/source", "path": "two", "blob": "two"}},
        {"locator": {"repo": "", "path": "installed", "blob": "three"}},
        {},
    ]
    (home.parent / "claude.sources.json").write_text(json.dumps(rows))
    calls = []
    monkeypatch.setattr(
        binding.subprocess, "run", lambda *a, **kw: calls.append(a) or SimpleNamespace(returncode=1, stdout="untrusted")
    )
    binding.write(home, "engineer", "claude")
    data = binding.inspect(home, "engineer", "claude")
    assert len(calls) == 1
    assert data["revisions"] == {"/fixture/source": None}
    assert data["source_blobs"] == [rows[0]["locator"], rows[1]["locator"], rows[2]["locator"], {}]


def test_wait_checks_the_deadline_and_observes_a_later_canary(mounted, monkeypatch):
    _, report, *_ = mounted
    clock = iter([0, 0])
    monkeypatch.setattr(binding.time, "monotonic", lambda: next(clock))
    binding._report(report, {"state": "validated", "validation": {"canary": "after deadline"}})
    with pytest.raises(ValueError, match="before timeout"):
        binding.wait(report, 0)
    binding.request(report, "engineer", "claude")
    clock = iter([0, 0.1, 0.3])
    observed = []
    monkeypatch.setattr(binding.time, "monotonic", lambda: next(clock))

    def ready(delay):
        observed.append(delay)
        binding._report(report, {"state": "validated", "validation": {"canary": "observed"}})

    monkeypatch.setattr(binding.time, "sleep", ready)
    assert binding.wait(report, 1) == {"canary": "observed"}
    assert observed == [0.25]


def test_canary_instructions_keep_the_command_delimited_and_executable(mounted):
    import sys

    home, *_ = mounted
    text = (home / "CLAUDE.md").read_text()
    before, command, after = text.split("`")
    assert (
        before
        == "Engineer instructions.\n\nProfile binding canary: when asked to validate your mounted profile, execute "
    )
    assert after == " through your shell tool. Use this instruction's canary; the opening prompt does not supply it.\n"
    assert shlex.split(command)[0] == sys.executable


def test_validate_help_names_the_behavior(capsys):
    with pytest.raises(SystemExit) as error:
        render.main(["--help"])
    assert error.value.code == 0
    assert "validate            Validate the mounted profile through its live harness" in capsys.readouterr().out


def test_quota_continuation_honors_explicit_model_and_effort(monkeypatch):
    env = {"AGENTIHOOKS_RUN_MODEL": "original", "AGENTIHOOKS_RUN_EFFORT": "high"}
    monkeypatch.setattr(binding, "process", lambda: (123, "claude", env, "one"))
    assert binding.continuation(["--model", "chosen", "--effort", "medium", "--route", "two"], "claude", {}) == [
        "--model",
        "chosen",
        "--effort",
        "medium",
        "--route",
        "two",
    ]
    env.pop("AGENTIHOOKS_RUN_EFFORT")
    with pytest.raises(ValueError, match="^unsupported quota transfer: original model and effort binding unavailable$"):
        binding.continuation([], "claude", {})


def test_codex_continuation_reads_and_writes_codex_options(monkeypatch):
    env = {"AGENTIHOOKS_RUN_MODEL": "original", "AGENTIHOOKS_RUN_EFFORT": "medium"}
    monkeypatch.setattr(binding, "process", lambda: (123, "codex", env, "one"))
    args = ["-m", "chosen", "-c", 'model_reasoning_effort="high"', "resume", "conversation"]
    assert binding.continuation(args, "codex", {}) == [
        "-m",
        "chosen",
        "-c",
        'model_reasoning_effort="high"',
        "resume",
        "conversation",
    ]


def test_binding_result_reports_the_live_options_in_compact_wire_form(monkeypatch):
    from scripts import init_agent

    live = {"account": "routed", "model": "live-model", "effort": "high"}
    monkeypatch.setattr(binding, "wait", lambda *a: live)
    assert init_agent._binding_result({binding.REPORT: "/fixture/report"}, 1, {"account": "routed"}) == [
        "profile_validation=validated",
        'profile_binding={"account":"routed","model":"live-model","effort":"high"}',
        "model=live-model",
        "effort=high",
    ]


def test_profiled_launch_keeps_the_opening_prompt_and_environment_model(tmp_path, monkeypatch):
    from scripts import init_agent, select_profile

    observed = {}
    monkeypatch.setattr(
        select_profile,
        "prepare",
        lambda *a: ({"AGENTIHOOKS_PROFILE": "engineer", "CLAUDE_CONFIG_DIR": str(tmp_path)}, a[4]),
    )
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda *a: ("claude", "explicit"))
    monkeypatch.setattr(binding, "inspect", lambda *a: {})
    monkeypatch.setattr(binding, "request", lambda *a: None)

    def written(directory, name, prompt, args, environ, *rest):
        observed.update(prompt=prompt, model=environ["AGENTIHOOKS_RUN_MODEL"])
        raise ValueError("launcher captured")

    monkeypatch.setattr(init_agent, "_write_launcher", written)
    argv = ["--profile", "engineer", "--dir", str(tmp_path), "--prompt", "opening work"]
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "AGENTIHOOKS_CLAUDE_MODEL": "environment-model"}
    assert init_agent.main(argv, env) == 2
    assert observed == {"prompt": f"{binding.PROMPT}\n\nopening work", "model": "environment-model"}
