"""The operating system safety nudge: master registry, one nudge round, the hourly job install and its check."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import cli_delegates
from scripts.swarm import safety_nudge


def _proc(tmp_path: Path, *pids: int) -> Path:
    proc = tmp_path / "proc"
    for pid in pids:
        (proc / str(pid)).mkdir(parents=True)
    proc.mkdir(exist_ok=True)
    return proc


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"AGENTIHOOKS_HOME": str(tmp_path / "home"), **extra}


def _master(tmp_path: Path, name: str, pane: str, target: str = "") -> dict[str, str]:
    extra = {"AGENTIHOOKS_TARGET": target} if target else {}
    return _env(tmp_path, AGENTIHOOKS_AGENT_NAME=name, AGENTIHOOKS_SWARM="demo", HERDR_PANE_ID=pane, **extra)


class FakeHerdr:
    def __init__(self, statuses: dict[str, str], typed: dict[str, str] | None = None) -> None:
        self.statuses = statuses
        self.typed = typed or {}
        self.prompts: list[tuple[str, str, str]] = []

    def agent_status(self, agent: safety_nudge.Pane) -> str:
        return self.statuses[agent.pane_id]

    def typed_input(self, agent: safety_nudge.Pane) -> str:
        return self.typed.get(agent.pane_id, "")

    def prompt(self, agent: safety_nudge.Pane, text: str) -> None:
        self.prompts.append((agent.pane_id, agent.harness, text))


class FakeCron:
    def __init__(self, lines: list[str] | None) -> None:
        self.lines = lines

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[0] == safety_nudge.CRON
        if args[1] == "-l":
            if self.lines is None:
                return subprocess.CompletedProcess(args, 1, "", "no table for user")
            return subprocess.CompletedProcess(args, 0, "".join(f"{line}\n" for line in self.lines), "")
        self.lines = str(kwargs["input"]).splitlines()
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.mark.feature("master-safety-nudge")
def test_a_master_in_a_herdr_pane_is_recorded_and_dead_masters_are_dropped(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 11, 22)
    safety_nudge.record(_master(tmp_path, "master@abc123-0001", "p1"), pid=11, now=100, proc=proc)
    (proc / "11").rmdir()
    safety_nudge.record(_master(tmp_path, "master@abc123-0002", "p2", "codex"), pid=22, now=200, proc=proc)

    entries = json.loads((tmp_path / "home" / safety_nudge.REGISTRY).read_text())

    assert entries == {
        "master@abc123-0002": {
            "seat": "master@abc123-0002",
            "slug": "demo",
            "harness": "codex",
            "pane": "p2",
            "pid": 22,
            "last_seen": 200,
        }
    }


@pytest.mark.feature("master-safety-nudge")
def test_a_session_that_is_not_a_master_in_a_pane_is_never_recorded(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 11)
    worker = _master(tmp_path, "ci@abc123-0003", "p1")
    outside = _env(tmp_path, AGENTIHOOKS_AGENT_NAME="master@abc123-0001", AGENTIHOOKS_SWARM="demo")

    assert safety_nudge.record(worker, pid=11, now=1, proc=proc) is None
    assert safety_nudge.record(outside, pid=11, now=1, proc=proc) is None
    assert not (tmp_path / "home" / safety_nudge.REGISTRY).exists()


@pytest.mark.feature("master-safety-nudge")
def test_an_unreadable_registry_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "home" / safety_nudge.REGISTRY
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    assert safety_nudge.load(path) == {}


@pytest.mark.feature("master-safety-nudge")
def test_a_round_nudges_idle_claude_and_codex_masters_and_skips_busy_or_typed_panes(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 1, 2, 3, 4, 5)
    for pid, pane, target in ((1, "p1", ""), (2, "p2", "codex"), (3, "p3", ""), (4, "p4", ""), (5, "p5", "")):
        safety_nudge.record(_master(tmp_path, f"master@abc123-000{pid}", pane, target), pid=pid, now=10, proc=proc)
    (proc / "5").rmdir()
    herdr = FakeHerdr({"p1": "idle", "p2": "done", "p3": "working", "p4": "idle"}, typed={"p4": "half a line"})

    results = safety_nudge.run_round(_env(tmp_path), herdr=herdr, now=3600, proc=proc)

    assert herdr.prompts == [("p1", "claude", safety_nudge.TEXT), ("p2", "codex", safety_nudge.TEXT)]
    assert {r["pane"]: r["outcome"] for r in results} == {
        "p1": "sent",
        "p2": "sent",
        "p3": "skipped-working",
        "p4": "skipped-typed",
    }
    logged = [json.loads(line) for line in (tmp_path / "home" / "logs" / safety_nudge.LOG).read_text().splitlines()]
    assert [(e["pane"], e["outcome"], e["at"]) for e in logged] == [(r["pane"], r["outcome"], 3600) for r in results]
    assert "master@abc123-0005" not in json.loads((tmp_path / "home" / safety_nudge.REGISTRY).read_text())


@pytest.mark.feature("master-safety-nudge")
def test_safety_nudge_text_is_the_operator_words() -> None:
    assert safety_nudge.TEXT == "Safety Nudge: In case the main tick or controller is down"


@pytest.mark.feature("master-safety-nudge")
def test_a_herdr_failure_is_logged_as_failed_and_the_round_goes_on(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 1, 2)
    for pid in (1, 2):
        safety_nudge.record(_master(tmp_path, f"master@abc123-000{pid}", f"p{pid}"), pid=pid, now=10, proc=proc)

    class Broken(FakeHerdr):
        def agent_status(self, agent: safety_nudge.Pane) -> str:
            if agent.pane_id == "p1":
                raise RuntimeError("herdr gone")
            return "idle"

    herdr = Broken({})
    results = safety_nudge.run_round(_env(tmp_path), herdr=herdr, now=20, proc=proc)

    assert [r["outcome"] for r in results] == ["failed-RuntimeError", "sent"]


@pytest.mark.feature("master-safety-nudge")
def test_install_registers_one_hourly_round_and_keeps_other_entries(tmp_path: Path) -> None:
    cron = FakeCron(["5 4 * * * backup", f"0 * * * * old {safety_nudge.MARKER}"])

    line = safety_nudge.install({"PATH": "/usr/bin"}, runner=cron)
    safety_nudge.install({"PATH": "/usr/bin"}, runner=cron)

    assert cron.lines == ["5 4 * * * backup", line]
    assert line.startswith("0 * * * * ")
    assert "safety-nudge round" in line and line.endswith(safety_nudge.MARKER)


@pytest.mark.feature("master-safety-nudge")
def test_install_works_for_a_user_with_no_table(tmp_path: Path) -> None:
    cron = FakeCron(None)

    line = safety_nudge.install({"PATH": "/usr/bin"}, runner=cron)

    assert cron.lines == [line]


@pytest.mark.feature("master-safety-nudge")
def test_check_reports_the_job_and_the_pane_of_this_master(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 1, 2)
    me = _master(tmp_path, "master@abc123-0001", "p1")
    safety_nudge.record(me, pid=1, now=10, proc=proc)
    safety_nudge.record(_master(tmp_path, "master@abc123-0002", "p2"), pid=2, now=10, proc=proc)

    missing = safety_nudge.check(me, runner=FakeCron([]), proc=proc)
    found = safety_nudge.check(me, runner=FakeCron([f"0 * * * * x {safety_nudge.MARKER}"]), proc=proc)

    assert missing["installed"] is False
    assert found["installed"] is True and found["job"].endswith(safety_nudge.MARKER)
    assert [m["pane"] for m in found["masters"]] == ["p1"]


@pytest.mark.feature("master-safety-nudge")
def test_agentihooks_safety_nudge_is_a_delegated_command() -> None:
    assert cli_delegates.delegated_cli(["safety-nudge", "round"]) is safety_nudge.main


@pytest.mark.feature("master-safety-nudge")
def test_home_defaults_to_the_agentihooks_folder_under_the_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert safety_nudge.home({}) == tmp_path / ".agentihooks"
    assert safety_nudge.registry_path({}) == tmp_path / ".agentihooks" / safety_nudge.REGISTRY
    assert safety_nudge.log_path({}) == tmp_path / ".agentihooks" / "logs" / safety_nudge.LOG


@pytest.mark.feature("master-safety-nudge")
def test_only_positive_process_ids_with_a_proc_entry_are_alive(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 0, 1)

    assert safety_nudge.alive(1, proc) is True
    assert safety_nudge.alive(0, proc) is False
    assert safety_nudge.alive(2, proc) is False


@pytest.mark.feature("master-safety-nudge")
def test_an_entry_without_a_process_id_is_dropped(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 0, 1)

    assert safety_nudge.live({"a": {"seat": "a"}, "b": {"seat": "b", "pid": 1}}, proc) == {"b": {"seat": "b", "pid": 1}}


@pytest.mark.feature("master-safety-nudge")
def test_load_keeps_only_object_entries_of_an_object_file(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"a": {"pid": 1}, "b": 3}))
    listed = tmp_path / "listed.json"
    listed.write_text("[1, 2]")

    assert safety_nudge.load(path) == {"a": {"pid": 1}}
    assert safety_nudge.load(listed) == {}
    assert safety_nudge.load(tmp_path / "missing.json") == {}


@pytest.mark.feature("master-safety-nudge")
def test_record_takes_the_agent_process_when_no_pid_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.context import account_sessions

    proc = _proc(tmp_path, 4242)
    monkeypatch.setattr(account_sessions, "agent_pid", lambda: 4242)

    entry = safety_nudge.record(_master(tmp_path, "master@abc123-0001", "p1"), now=7, proc=proc)

    assert entry is not None and entry["pid"] == 4242
    assert safety_nudge.load(tmp_path / "home" / safety_nudge.REGISTRY)["master@abc123-0001"]["pid"] == 4242


@pytest.mark.feature("master-safety-nudge")
def test_a_round_with_no_live_master_logs_no_masters_and_rounds_append(tmp_path: Path) -> None:
    herdr = FakeHerdr({})
    proc = _proc(tmp_path)

    assert safety_nudge.run_round(_env(tmp_path), herdr=herdr, now=1, proc=proc) == []
    assert safety_nudge.run_round(_env(tmp_path), herdr=herdr, now=2, proc=proc) == []

    lines = (tmp_path / "home" / "logs" / safety_nudge.LOG).read_text().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"at": 1, "outcome": "no-masters"},
        {"at": 2, "outcome": "no-masters"},
    ]
    assert herdr.prompts == []


def _tool(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    tool = folder / name
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    return tool


@pytest.mark.feature("master-safety-nudge")
def test_the_cron_line_reaches_agentihooks_herdr_and_the_herdr_socket(tmp_path: Path) -> None:
    exe = _tool(tmp_path / "a", "agentihooks")
    _tool(tmp_path / "h", "herdr")
    environ = {"PATH": f"{tmp_path / 'a'}:{tmp_path / 'h'}", "HERDR_SOCKET_PATH": "/run/h.sock"}

    line = safety_nudge.command(environ)

    assert line == (
        f"0 * * * * PATH={tmp_path / 'a'}:{tmp_path / 'h'}:/usr/local/bin:/usr/bin:/bin "
        f"HERDR_SOCKET_PATH=/run/h.sock {exe} safety-nudge round >/dev/null 2>&1 {safety_nudge.MARKER}"
    )


@pytest.mark.feature("master-safety-nudge")
def test_the_cron_line_falls_back_to_the_bare_command_when_nothing_is_found(tmp_path: Path) -> None:
    line = safety_nudge.command({"PATH": str(tmp_path / "empty")})

    assert line == (
        "0 * * * * PATH=/usr/local/bin:/usr/bin:/bin agentihooks safety-nudge round >/dev/null 2>&1 "
        f"{safety_nudge.MARKER}"
    )


class StrictCron(FakeCron):
    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["capture_output"] is True and kwargs["text"] is True
        return super().__call__(args, **kwargs)


@pytest.mark.feature("master-safety-nudge")
def test_cron_reads_and_writes_capture_text_output() -> None:
    cron = StrictCron(["5 4 * * * backup"])

    line = safety_nudge.install({"PATH": "/usr/bin"}, runner=cron)

    assert safety_nudge.table(cron) == ["5 4 * * * backup", line]


@pytest.mark.feature("master-safety-nudge")
def test_install_raises_with_the_cron_error_when_the_write_is_refused() -> None:
    def refuse(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[1] == "-l":
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", " bad minute \n")

    with pytest.raises(RuntimeError) as raised:
        safety_nudge.install({"PATH": "/usr/bin"}, runner=refuse)

    assert str(raised.value) == f"{safety_nudge.CRON} refused the entry: bad minute"


@pytest.mark.feature("master-safety-nudge")
def test_check_lists_every_live_master_for_a_caller_that_is_not_registered(tmp_path: Path) -> None:
    proc = _proc(tmp_path, 1, 2, 3)
    for pid in (1, 2, 3):
        safety_nudge.record(_master(tmp_path, f"master@abc123-000{pid}", f"p{pid}"), pid=pid, now=10, proc=proc)
    (proc / "3").rmdir()

    report = safety_nudge.check(_env(tmp_path), runner=FakeCron(None), proc=proc)

    assert (report["installed"], report["job"]) == (False, "")
    assert [m["pane"] for m in report["masters"]] == ["p1", "p2"]


@pytest.mark.feature("master-safety-nudge")
@pytest.mark.parametrize(
    ("action", "patched", "value", "code"),
    [
        ("round", "run_round", [{"outcome": "sent"}], 0),
        ("record", "record", {"seat": "s"}, 0),
        ("check", "check", {"installed": True}, 0),
        ("check", "check", {"installed": False}, 1),
    ],
)
def test_main_runs_each_action_and_prints_its_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    patched: str,
    value: object,
    code: int,
) -> None:
    seen: list[dict[str, str]] = []

    def fake(environ: dict[str, str]) -> object:
        seen.append(environ)
        return value

    monkeypatch.setenv("AGENTIHOOKS_SAFETY_PROBE", "on")
    monkeypatch.setattr(safety_nudge, patched, fake)

    assert safety_nudge.main([action]) == code
    assert json.loads(capsys.readouterr().out) == value
    assert seen[0]["AGENTIHOOKS_SAFETY_PROBE"] == "on"


@pytest.mark.feature("master-safety-nudge")
def test_main_install_prints_the_installed_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(safety_nudge, "install", lambda environ: "the line")

    assert safety_nudge.main(["install"]) == 0
    assert json.loads(capsys.readouterr().out) == {"installed": "the line"}


@pytest.mark.feature("master-safety-nudge")
def test_main_refuses_an_unknown_action() -> None:
    with pytest.raises(SystemExit):
        safety_nudge.main(["sweep"])


@pytest.mark.feature("master-safety-nudge")
def test_the_ledger_adapter_records_the_master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger.adapters import safety_nudge as adapter

    seen: list[object] = []
    monkeypatch.setattr(safety_nudge, "record", seen.append)
    environ = _master(tmp_path, "master@abc123-0001", "p1")

    adapter.record_master(environ)

    assert seen == [environ]


@pytest.mark.feature("master-safety-nudge")
@pytest.mark.parametrize(("role", "recorded"), [("orchestrator", 1), ("member", 0)])
def test_only_an_orchestrator_join_records_the_master(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], role: str, recorded: int
) -> None:
    from types import SimpleNamespace

    from scripts.swarm_ledger import ledger
    from scripts.swarm_ledger.adapters import safety_nudge as adapter

    seen: list[object] = []
    monkeypatch.setattr(ledger, "send", lambda *args, **kwargs: {})
    monkeypatch.setattr(adapter, "record_master", seen.append)

    ledger.cmd_join(SimpleNamespace(slug="demo", name="master@abc123-0001", role=role))

    assert len(seen) == recorded
    assert json.loads(capsys.readouterr().out)["role"] == role
