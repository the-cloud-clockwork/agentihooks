import json
from pathlib import Path
from typing import Any

import pytest

from scripts.profiles import binding
from scripts.swarm import runtime

pytestmark = pytest.mark.feature("codex-planner-sandbox-canary")


@pytest.fixture
def mounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, dict[str, Any]]:
    home = tmp_path / "planner" / "codex"
    home.mkdir(parents=True)
    (home / binding.PERSONAS["codex"]).write_text(binding.persona("Use the planner persona.\n"))
    (home.parent / "codex.sources.json").write_text("[]")
    binding.write(home, "planner", "codex")
    report = tmp_path / "report.json"
    binding.request(report, "planner", "codex", home)
    launched: dict[str, Any] = {
        "pid": 2345,
        "start": "98765",
        "thread": "thread-planner",
        "profile": "planner",
        "harness": "codex",
        "home": str(home),
        "report": str(report),
        "account": "account-one",
        "model": "gpt-6.1-sol",
        "effort": "high",
    }
    requested = json.loads(report.read_text())
    requested["sandbox_launch"] = launched
    report.write_text(json.dumps(requested))
    monkeypatch.setenv(binding.REPORT, str(report))
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "planner")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-planner")
    monkeypatch.setattr(binding.os, "getppid", lambda: 1)
    return home, report, launched


def test_a_sandboxed_planner_validates_its_recorded_outer_launch(
    mounted: tuple[Path, Path, dict[str, Any]],
) -> None:
    home, report, launched = mounted
    canary = binding.inspect(home, "planner", "codex")["canary"]
    result = binding.validate(canary)
    assert result["state"] == "validated"
    assert result["pid"] == launched["pid"]
    assert result["account"] == launched["account"]
    assert result["model"] == launched["model"]
    assert result["effort"] == launched["effort"]
    assert result["home"] == str(home)
    assert json.loads(report.read_text())["validation"] == result
    assert binding.confirm(report, "planner", "codex", "account-one") == result
    assert binding.validate(canary)["pid"] == launched["pid"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENTIHOOKS_PROFILE", "engineer"),
        ("CODEX_HOME", "/other/home"),
        ("CODEX_THREAD_ID", "thread-other"),
    ],
)
def test_a_sandboxed_canary_refuses_another_execution(
    mounted: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    home, _, _ = mounted
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        binding.validate(binding.inspect(home, "planner", "codex")["canary"])


@pytest.mark.parametrize("field", ["sandbox_launch", "pid", "start", "thread", "profile", "harness", "home", "report"])
def test_a_sandboxed_canary_requires_the_complete_launch_record(
    mounted: tuple[Path, Path, dict[str, Any]], field: str
) -> None:
    home, report, _ = mounted
    requested = json.loads(report.read_text())
    if field == "sandbox_launch":
        requested.pop(field)
    else:
        requested["sandbox_launch"].pop(field)
    report.write_text(json.dumps(requested))
    with pytest.raises(ValueError):
        binding.validate(binding.inspect(home, "planner", "codex")["canary"])


@pytest.mark.parametrize(
    ("field", "value"), [("harness", "claude"), ("profile", "engineer"), ("report", "/other/report")]
)
def test_a_sandboxed_canary_refuses_a_foreign_launch_record(
    mounted: tuple[Path, Path, dict[str, Any]], field: str, value: str
) -> None:
    home, report, _ = mounted
    requested = json.loads(report.read_text())
    requested["sandbox_launch"][field] = value
    report.write_text(json.dumps(requested))
    with pytest.raises(ValueError):
        binding.validate(binding.inspect(home, "planner", "codex")["canary"])


def test_a_sandboxed_planner_keeps_canary_and_revalidation_checks(
    mounted: tuple[Path, Path, dict[str, Any]],
) -> None:
    home, report, _ = mounted
    with pytest.raises(ValueError, match="canary mismatch"):
        binding.validate("other-canary")
    canary = binding.inspect(home, "planner", "codex")["canary"]
    binding.validate(canary)
    with pytest.raises(ValueError, match="account differs"):
        binding.confirm(report, "planner", "codex", "other-account")
    requested = json.loads(report.read_text())
    requested["sandbox_launch"]["pid"] += 1
    report.write_text(json.dumps(requested))
    with pytest.raises(ValueError, match="binding changed"):
        binding.validate(canary)


def test_the_supervisor_records_the_launch_before_the_canary_turn(
    mounted: tuple[Path, Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from scripts.codex_session import supervisor

    home, report, launched = mounted
    requested = json.loads(report.read_text())
    requested.pop("sandbox_launch")
    report.write_text(json.dumps(requested))
    env = {
        binding.REPORT: str(report),
        "AGENTIHOOKS_PROFILE": "planner",
        "CODEX_HOME": str(home),
        "AGENTIHOOKS_RUN_MODEL": "gpt-6.1-sol",
        "AGENTIHOOKS_RUN_EFFORT": "high",
    }

    def live_process(proc: Path = Path("/proc"), start: int | None = None) -> tuple[int, str, dict[str, str], str]:
        if start is None:
            raise ValueError("profile canary has no supported live harness ancestor")
        assert start == launched["pid"]
        return start, "codex", env, "account-one"

    monkeypatch.setattr(binding, "process", live_process)
    connection = Mock()
    monkeypatch.setattr(supervisor, "wait_ready", lambda *args: (connection, {}))
    monkeypatch.setattr(supervisor, "check_binding", lambda *args: None)
    monkeypatch.setattr(supervisor, "bind_thread", lambda *args: {"id": "thread-planner"})
    monkeypatch.setattr(supervisor, "name_thread", lambda *args: None)
    opened = []

    def open_turn(*args: Any) -> str:
        snapshot = json.loads(report.read_text())["sandbox_launch"]
        assert snapshot["pid"] == launched["pid"]
        assert snapshot["start"] == 98765
        assert snapshot["thread"] == "thread-planner"
        result = binding.validate(binding.inspect(home, "planner", "codex")["canary"])
        assert result["account"] == "account-one"
        opened.append(result)
        return "turn-planner"

    monkeypatch.setattr(supervisor, "open_turn", open_turn)
    agent = supervisor.AgentRecord("planner-test", "plan", "plan-task", execution_id="execution")
    launch = supervisor.Launch("swarm", agent, str(home), str(home), env, opening="Start.")
    record = supervisor.TransportRecord("execution", 1, "plan-seat", agent.name, agent.task, "unix://endpoint")
    process = Mock(pid=launched["pid"])
    record = supervisor.replace(record, server_start_identity=98765)
    result, _ = supervisor._connect_bound(Mock(), Mock(), launch, agent, record, process, Mock(now_ms=lambda: 1000))
    assert result.active_turn_id == "turn-planner"
    assert len(opened) == 1


def test_planner_permissions_keep_the_repository_read_only(tmp_path: Path) -> None:
    args = runtime.codex_plan_mode({"HOME": str(tmp_path)})
    assert args == [
        "-c",
        'permissions.planner={extends=":read-only", network={enabled=true}, filesystem={'
        f'"{tmp_path}/development-ledger"="write", '
        f'"{tmp_path}/.agentihooks/swarm"="write", "{tmp_path}/scratchpad"="write"}}}}',
        "-c",
        'default_permissions="planner"',
    ]


@pytest.mark.parametrize(("target", "profile"), [("codex", "planner"), ("codex", "engineer"), ("claude", "planner")])
def test_only_codex_planner_reports_move_into_the_existing_write_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str, profile: str
) -> None:
    from argparse import Namespace

    from scripts import init_agent

    home = tmp_path / profile / target
    home.mkdir(parents=True)
    (home / binding.PERSONAS[target]).write_text(binding.persona("Use the selected persona.\n"))
    (home.parent / f"{target}.sources.json").write_text("[]")
    binding.write(home, profile, target)
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setattr(init_agent, "_runtime_dir", lambda environ: runtime_dir)
    monkeypatch.setattr(init_agent, "model_effort", lambda *args: ("model", "high"))
    env = {"HOME": str(tmp_path / "user"), binding.HOMES[target]: str(home)}
    init_agent._binding_request(Namespace(profile=profile, defer_canary=False), target, "Start.", env, [])
    report = Path(env[binding.REPORT])
    expected = tmp_path / "user" / ".agentihooks" / "swarm" / "profile-reports"
    assert report.parent == (expected if (target, profile) == ("codex", "planner") else runtime_dir)
    assert report.is_file()
    assert report.stat().st_mode & 0o777 == 0o600
    assert f"profile_report={report}" in capsys.readouterr().out
