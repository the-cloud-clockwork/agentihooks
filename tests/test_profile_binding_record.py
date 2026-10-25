import json

import pytest

from scripts.profiles import binding, render

PROCESS = binding.process


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "engineer" / "claude"
    home.mkdir(parents=True)
    (home / "CLAUDE.md").write_text(binding.persona("Engineer instructions.\n"))
    (home.parent / "claude.sources.json").write_text("[]")
    binding.write(home, "engineer", "claude")
    return home


def _session(tmp_path, home, items):
    report = tmp_path / "report.json"
    binding.request(report, "engineer", "claude")
    proc = tmp_path / "proc"
    shell, agent = proc / "3", proc / "2"
    shell.mkdir(parents=True)
    agent.mkdir()
    (shell / "comm").write_text("bash\n")
    (shell / "status").write_text("Name: bash\nPPid: 2\n")
    (agent / "comm").write_text("claude\n")
    env = [*items, "AGENTIHOOKS_PROFILE=engineer", f"{binding.REPORT}={report}", f"CLAUDE_CONFIG_DIR={home}"]
    (agent / "environ").write_bytes("\0".join(env).encode())
    (agent / "cmdline").write_bytes(b"claude")
    return lambda: PROCESS(proc, 3)


def test_binding_command_prints_the_session_record_fields(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(binding, "process", _session(tmp_path, home, ["AH_CC_TOKEN_fixture=x"]))
    assert binding.main(binding.inspect(home, "engineer", "claude")["canary"]) == 0
    capsys.readouterr()
    assert render.main(["binding"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": "engineer",
        "harness": "claude",
        "persona": binding.digest(home / "CLAUDE.md"),
        "sources": binding.digest(home.parent / "claude.sources.json"),
        "state": "validated",
        "account": "fixture",
    }


def test_an_api_route_is_attributed_and_its_binding_accepted(tmp_path, home, monkeypatch, capsys):
    from scripts import init_agent

    sentinel = "sentinel-value-7f3a"
    process = _session(tmp_path, home, ["AH_ROUTE_API=1", "ANTHROPIC_BASE_URL=" + sentinel])
    assert process()[3] == "api"
    monkeypatch.setattr(binding, "process", process)
    assert binding.main(binding.inspect(home, "engineer", "claude")["canary"]) == 0
    assert sentinel not in capsys.readouterr().out
    report = tmp_path / "report.json"
    lines = init_agent._binding_result({binding.REPORT: str(report)}, 1, {"account": "api"})
    assert lines[0] == "profile_validation=validated"
    assert json.loads(lines[1].removeprefix("profile_binding="))["account"] == "api"
    assert sentinel not in "\n".join(lines)
    with pytest.raises(ValueError, match="^live process account differs from requested route$"):
        init_agent._binding_result({binding.REPORT: str(report)}, 1, {"account": "alpha"})


def test_binding_command_reads_any_home_and_reports_a_stale_record(home, capsys):
    assert render.main(["binding", "--home", str(home)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["profile"], shown["state"], shown["account"]) == ("engineer", "rendered", None)
    (home / "CLAUDE.md").write_text("Changed persona.\n")
    assert render.main(["binding", "--home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "invalid: mounted persona changed since render"


def test_binding_command_prints_no_environment_value(tmp_path, home, monkeypatch, capsys):
    secrets = ("sk-secret-token-one", "sk-secret-key-two", "secret-run-model")
    items = [
        f"AH_CC_TOKEN_fixture={secrets[0]}",
        f"ANTHROPIC_API_KEY={secrets[1]}",
        f"AGENTIHOOKS_RUN_MODEL={secrets[2]}",
    ]
    monkeypatch.setattr(binding, "process", _session(tmp_path, home, items))
    monkeypatch.setenv("AH_CC_TOKEN_fixture", "sk-secret-env-three")
    assert render.main(["binding"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["account"] == "fixture"
    for value in (*secrets, "sk-secret-env-three", str(home), str(tmp_path / "report.json")):
        assert value not in captured.out + captured.err


def test_profile_check_failure_names_the_binding_command(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(binding, "process", _session(tmp_path, home, []))
    assert binding.main("wrong") == 2
    assert capsys.readouterr().err == (
        "profile validation failed: mounted instruction canary mismatch; "
        "read the binding record with agentihooks profile binding\n"
    )


def test_binding_command_reports_a_failed_check_and_a_missing_home(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(binding, "process", _session(tmp_path, home, []))
    assert binding.main("wrong") == 2
    capsys.readouterr()
    assert render.main(["binding"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "failed: mounted instruction canary mismatch"
    monkeypatch.setattr(binding, "process", lambda: (2, "claude", {}, "fixture"))
    assert render.main(["binding"]) == 1
    assert capsys.readouterr().err == "ERROR: missing profile home: CLAUDE_CONFIG_DIR is unset\n"


def test_binding_command_help_names_its_record_and_home(capsys):
    with pytest.raises(SystemExit):
        render.main(["--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "binding Print a profile home's binding record, never an environment value" in help_text
    with pytest.raises(SystemExit):
        render.main(["binding", "--help"])
    assert "--home HOME Profile home to read (default: this session's)" in " ".join(capsys.readouterr().out.split())
    assert render.main(["binding", "--home", "relative-missing"]) == 1
    assert capsys.readouterr().err.startswith("ERROR: [Errno 2] No such file or directory: 'relative-missing/")
