import json
import shlex
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import init_agent, master_claude_login
from scripts.swarm import remote_control
from scripts.swarm.health import findings
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import Placed, placed_record
from tests.swarm.test_runtime import _passed, _resuming, validated

RC = ["--remote-control", "master 1 · proof", "--remote-control-session-name-prefix", "hive-a"]
ON = {"state": "on", "detail": "master 1 · proof"}


@pytest.fixture(autouse=True)
def hive(monkeypatch):
    from scripts.routing import native_login

    monkeypatch.setenv("SWARM_HIVE_ID", "hive-a")
    monkeypatch.setattr(native_login, "check", lambda *_: {"loggedIn": True, "authMethod": "claude.ai"})
    monkeypatch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", True)


def _config(tmp_path, lane, harness):
    return SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": harness}},
        autonomy="delegate",
    )


def _spawn(tmp_path, lane, harness, kind, login=True, seat=None):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "requested"))
    task = {"id": lane, "title": "proof", "route_kind": kind} | ({"seat": seat} if seat is not None else {})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", login)
        placed = runtime.spawn(_config(tmp_path, lane, harness), lane, "agent", task)
    return _passed(seen[0]), placed


def _rc(passed):
    if "--remote-control" not in passed:
        return []
    at = passed.index("--remote-control")
    return passed[at : at + 4]


def test_a_master_with_the_login_on_spawns_on_its_own_route_with_remote_control(tmp_path):
    passed, placed = _spawn(tmp_path, "master", "claude", "interactive")
    assert passed[:6] == ["--route", "claude-ai", *RC]
    assert (placed.route_kind, placed.remote_control) == ("interactive", ON)


@pytest.mark.parametrize("kind", ["subscription", "api"])
def test_a_front_door_order_on_an_explicit_route_takes_the_login_and_remote_control(tmp_path, kind):
    passed, placed = _spawn(tmp_path, "master", "claude", kind)
    assert passed[:6] == ["--route", "claude-ai", *RC]
    assert (placed.route_kind, placed.remote_control) == ("interactive", ON)


@pytest.mark.parametrize(("kind", "route"), [("interactive", ["--route", "interactive"]), ("subscription", [])])
def test_a_master_with_the_login_off_spawns_as_today_without_remote_control(tmp_path, kind, route):
    passed, placed = _spawn(tmp_path, "master", "claude", kind, login=False)
    assert passed[: len(route)] == route
    assert "master-login" not in passed and "--settings" not in passed and _rc(passed) == []
    assert (placed.route_kind, placed.remote_control) == (kind, {"state": "off", "detail": "flag"})


@pytest.mark.parametrize(("lane", "harness"), [("master", "codex"), ("eng", "claude"), ("ci", "claude")])
def test_codex_masters_and_workers_never_take_the_master_login(tmp_path, lane, harness):
    passed, placed = _spawn(tmp_path, lane, harness, "interactive")
    assert "master-login" not in passed and _rc(passed) == []
    assert placed.remote_control == {}


def test_the_setting_off_keeps_the_login_off(monkeypatch):
    monkeypatch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", False)
    assert master_claude_login.enabled("proof", "master@proof", "master", "claude") is False
    assert master_claude_login.route(False) == []


@pytest.mark.parametrize(("lane", "harness"), [("master", "claude"), ("eng", "claude"), ("master", "codex")])
def test_the_login_off_never_reaches_the_master_login_home(tmp_path, monkeypatch, lane, harness):
    for name in ("build", "claim", "prepare", "publish", "restore"):
        monkeypatch.setattr(master_claude_login, name, lambda *_: pytest.fail("master login touched"))
    passed, _ = _spawn(tmp_path, lane, harness, "subscription", login=False)
    assert not {"--settings", "master-login", "--remote-control"} & set(passed)


def test_a_master_placed_on_the_api_route_records_gateway(tmp_path):
    def run(argv, **kwargs):
        output = "status=started\nroute_status=routed\naccount=api\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, output), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    task = {"id": "master", "title": "proof"}
    placed = runtime.spawn(_config(tmp_path, "master", "claude"), "master", "agent", task)
    assert (placed.route_kind, placed.remote_control) == ("api", {"state": "off", "detail": "gateway"})


def _resumed(tmp_path, lane, harness, kind, login=True, seat=""):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness)
    config.slug = "proof"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", login)
        placed = runtime.resume(config, replace(agent, lane=lane, task=lane, route_kind=kind, seat=seat), "resume")
    return _passed(seen["runs"][0]), placed


@pytest.mark.parametrize("login", [True, False])
@pytest.mark.parametrize("kind", ["interactive", "subscription"])
def test_a_backend_master_spawns_without_the_master_login_or_remote_control(tmp_path, kind, login):
    passed, placed = _spawn(tmp_path, "master", "claude", kind, login=login, seat="master-2@proof")
    assert not {"master-login", "--settings", "--remote-control"} & set(passed)
    assert _rc(passed) == [] and placed.remote_control == {}
    assert placed.route_kind == kind


def test_the_front_door_seat_spawns_with_remote_control(tmp_path):
    passed, placed = _spawn(tmp_path, "master", "claude", "interactive", seat="master@proof")
    assert passed[:6] == ["--route", "claude-ai", *RC]
    assert placed.remote_control == ON


def test_a_backend_master_never_reaches_the_master_login_home(tmp_path, monkeypatch):
    for name in ("build", "claim", "prepare", "publish", "restore"):
        monkeypatch.setattr(master_claude_login, name, lambda *_, **__: pytest.fail("master login touched"))
    passed, _ = _spawn(tmp_path, "master", "claude", "interactive", seat="master-3@proof")
    assert passed[:2] == ["--route", "interactive"]


@pytest.mark.parametrize("login", [True, False])
def test_a_backend_master_resumes_without_the_master_login_or_remote_control(tmp_path, login):
    passed, placed = _resumed(tmp_path, "master", "claude", "interactive", login, seat="master-2@proof")
    assert passed[:2] == ["--route", "interactive"]
    assert not {"master-login", "--remote-control"} & set(passed)
    assert (placed.route_kind, placed.remote_control) == ("interactive", {})


def test_the_front_door_seat_resumes_with_remote_control(tmp_path):
    passed, placed = _resumed(tmp_path, "master", "claude", "subscription", seat="master@proof")
    assert passed[:6] == ["--route", "claude-ai", *RC]
    assert (placed.route_kind, placed.remote_control) == ("interactive", ON)


@pytest.mark.parametrize("kind", ["interactive", "subscription", "api"])
def test_a_master_with_the_login_on_resumes_on_its_own_route_with_remote_control(tmp_path, kind):
    passed, placed = _resumed(tmp_path, "master", "claude", kind)
    assert passed[:6] == ["--route", "claude-ai", *RC]
    assert (placed.route_kind, placed.remote_control) == ("interactive", ON)


@pytest.mark.parametrize(
    ("lane", "harness", "login", "decided"),
    [
        ("master", "claude", False, {"state": "off", "detail": "flag"}),
        ("master", "codex", True, {}),
        ("eng", "claude", True, {}),
    ],
)
def test_other_resumes_keep_their_route_and_carry_no_remote_control(tmp_path, lane, harness, login, decided):
    passed, placed = _resumed(tmp_path, lane, harness, "interactive", login)
    assert passed[:2] == ["--route", "interactive"] and _rc(passed) == []
    assert placed.remote_control == decided


def test_an_on_decision_names_the_front_door_seat_number_and_the_slug():
    assert remote_control.decision("master", "claude", "interactive", "rig", True) == {
        "state": "on",
        "detail": "master 1 · rig",
    }


@pytest.mark.parametrize("kind", ["interactive", "api", "subscription"])
def test_the_login_off_always_reports_flag(kind):
    assert remote_control.off_reason(kind, False) == "flag"


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("interactive", ""), ("api", "gateway"), ("subscription", "fallback auth"), ("", "fallback auth")],
)
def test_off_reason_by_route_kind(kind, reason):
    assert remote_control.off_reason(kind, True) == reason


def test_only_an_on_decision_produces_launch_arguments():
    assert remote_control.arguments(ON) == RC
    assert remote_control.arguments({"state": "off", "detail": "flag"}) == []
    assert remote_control.arguments({}) == []


@pytest.mark.parametrize(
    ("decided", "text"),
    [
        (ON, "Remote Control: on (master 1 · proof)"),
        ({"state": "off", "detail": "fallback auth"}, "Remote Control: off (fallback auth)"),
        ({}, ""),
        (None, ""),
    ],
)
def test_status_line_reads_the_recorded_decision(decided, text):
    assert remote_control.line({"remote_control": decided}) == text


def test_placement_records_the_decision_and_keeps_it_when_a_relaunch_has_none():
    agent = AgentRecord("master@a1b2c3-0001", "master", "master")
    placed = placed_record(agent, Placed("w1:p1", "claude", remote_control=ON))
    assert placed.remote_control == ON
    assert placed_record(placed, Placed("w1:p2", "claude")).remote_control == ON


@pytest.mark.parametrize(
    ("message", "failed"),
    [
        ("Remote Control failed · /remote-control", True),
        ("Remote Control disconnected — run /login to restore Remote Control", True),
        ("Remote Control stopped: the server refused to reconnect this computer", True),
        ("Remote Control session expired", True),
        ("Remote Control back on", False),
        ("Upload failed", False),
        ("Remote Control on. Upload failed", False),
        ("Claude needs your permission to use Bash", False),
        ("", False),
    ],
)
def test_only_remote_control_failure_notices_count(message, failed):
    assert remote_control.failure(message) is failed


def _master(**extra):
    return {"name": "master@a1b2c3-0001", "lane": "master", "seat": "master@proof", "started_at": 1_000, **extra}


def test_a_failure_since_the_master_started_is_a_master_rc_finding():
    rows = [_master(remote_control_failure={"message": "Remote Control failed", "at": 1_000})]
    found = findings.master_rc(rows)
    assert [f.id for f in found] == ["master-rc/master@proof"]
    assert found[0].summary == "Remote Control failed on the master, phone access lost"
    assert found[0].evidence == ("notice: Remote Control failed", "agent master@a1b2c3-0001")
    assert found[0].threshold == "a Remote Control failure notice since the master started"
    assert found[0].measure == 1


@pytest.mark.parametrize(
    "row",
    [
        _master(remote_control_failure={"message": "Remote Control failed", "at": 999}),
        _master(remote_control_failure=None),
        {**_master(remote_control_failure={"message": "Remote Control failed", "at": 5_000}), "lane": "eng"},
    ],
    ids=["before-start", "none", "not-master"],
)
def test_old_missing_or_worker_failures_raise_no_finding(row):
    assert findings.master_rc([row]) == []


def test_a_skipped_row_does_not_hide_a_later_master_failure():
    rows = [
        {**_master(remote_control_failure={"message": "m", "at": 5_000}), "lane": "eng", "name": "e"},
        _master(remote_control_failure={"message": "Remote Control failed", "at": 2_000}),
    ]
    assert [f.id for f in findings.master_rc(rows)] == ["master-rc/master@proof"]


def test_a_master_without_a_seat_or_start_is_named_by_agent():
    row = {"name": "master@a1b2c3-0001", "lane": "master", "remote_control_failure": {"message": "m", "at": 0}}
    assert [f.id for f in findings.master_rc([row])] == ["master-rc/master@a1b2c3-0001"]


def test_findings_include_the_master_rc_detector():
    ledger = {"tasks": [], "_meta": {"events": []}}
    rows = [_master(remote_control_failure={"message": "Remote Control failed", "at": 2_000})]
    found = findings.findings(ledger, rows, {}, findings.Limits())
    assert "master-rc/master@proof" in [f.id for f in found]


def test_a_master_launch_with_remote_control_still_marks_its_folder_trusted(monkeypatch, tmp_path, capsys):
    project = tmp_path / "fresh"
    project.mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {}}))
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(
        init_agent.subprocess, "Popen", lambda command, **kwargs: Path(command[-1]).with_suffix(".started").touch()
    )
    argv = ["--dir", str(project), "--name", "master", "--agent", "claude", "--start-timeout", "0"]
    rc = init_agent.main(
        [*argv, "--route-timeout", "0", "--", "--route", "interactive", *RC],
        {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
    )
    out = capsys.readouterr().out
    assert rc == 0 and "trust=marked" in out
    assert f"--remote-control {shlex.quote('master 1 · proof')}" in out
    assert json.loads((tmp_path / ".claude.json").read_text())["projects"][str(project)]["hasTrustDialogAccepted"]
