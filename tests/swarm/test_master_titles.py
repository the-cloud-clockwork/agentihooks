from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts import init_agent, master_claude_login, terminate_agent
from scripts.swarm import remote_control
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm_v2 import masters
from tests.swarm.test_runtime import _passed, _resuming, validated


@pytest.fixture(autouse=True)
def hive(monkeypatch):
    from scripts.routing import native_login

    monkeypatch.setenv("SWARM_HIVE_ID", "hive-a")
    monkeypatch.setattr(native_login, "check", lambda *_: {"loggedIn": True, "authMethod": "claude.ai"})
    monkeypatch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", True)


def _spawned(tmp_path, slug, lane, seat, kind=None):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    config = SimpleNamespace(
        slug=slug,
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={lane: {"agent": "claude"}},
        autonomy="delegate",
    )
    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "requested"))
    task = {"id": lane, "title": slug, "seat": seat} | ({"route_kind": kind} if kind else {})
    placed = runtime.spawn(config, lane, "agent", task)
    return seen[0], placed


def _title(argv):
    head = argv[: argv.index("--")]
    return head[head.index("--title") + 1] if "--title" in head else None


@pytest.mark.parametrize(
    ("address", "shown"),
    [
        ("master@rig", "master 1 · rig"),
        ("", "master 1 · rig"),
        ("master-2@rig", "master 2 · rig"),
        ("master-3@rig", "master 3 · rig"),
    ],
)
def test_a_master_title_carries_its_seat_number_and_its_swarm(address, shown):
    assert masters.title("rig", address) == shown
    assert masters.is_title(shown) is True


@pytest.mark.parametrize("text", ["master-323133-0188", "master · rig", "master 2 rig", "engineer 1 · rig", ""])
def test_only_a_seat_title_reads_as_one(text):
    assert masters.is_title(text) is False


def test_two_masters_of_one_swarm_launch_under_distinct_seat_titles(tmp_path):
    front, _ = _spawned(tmp_path, "rig", "master", "master@rig")
    backend, _ = _spawned(tmp_path, "rig", "master", "master-2@rig")
    assert (_title(front), _title(backend)) == ("master 1 · rig", "master 2 · rig")


def test_a_worker_launch_carries_no_title(tmp_path):
    argv, _ = _spawned(tmp_path, "rig", "eng", "eng-1@rig")
    assert _title(argv) is None


def test_a_resumed_backend_master_keeps_its_seat_title(tmp_path):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee")
    runtime.resume(config, replace(agent, lane="master", task="master", seat="master-2@sw"), "resume")
    assert _title(seen["runs"][0]) == "master 2 · sw"


@pytest.mark.parametrize("kind", [None, "interactive", "subscription", "api"])
def test_a_doctor_front_door_launch_registers_remote_control_under_its_seat(tmp_path, kind):
    argv, placed = _spawned(tmp_path, "rig-doctor", "master", "master@rig-doctor", kind)
    passed = _passed(argv)
    at = passed.index("--remote-control")
    assert passed[:2] == ["--route", master_claude_login.ROUTE]
    assert passed[at : at + 2] == ["--remote-control", "master 1 · rig-doctor"]
    assert (placed.route_kind, placed.remote_control) == (
        "interactive",
        {"state": "on", "detail": "master 1 · rig-doctor"},
    )
    assert _title(argv) == "master 1 · rig-doctor"


def test_an_on_decision_names_the_front_door_seat_and_the_swarm():
    assert remote_control.decision("master", "claude", "interactive", "doctor", True)["detail"] == "master 1 · doctor"


def test_the_title_names_the_claude_session_and_the_herdr_tab(tmp_path, monkeypatch):
    spec = init_agent.AgentSpec(title="master 2 · rig")
    command, _ = init_agent._agent_command(spec, tmp_path / "route", "master-a1b2c3-0002", [], {}, tmp_path)
    assert command[command.index("--name") + 1] == "master 2 · rig"
    plain, _ = init_agent._agent_command(init_agent.AgentSpec(), tmp_path / "route", "master-a1", [], {}, tmp_path)
    assert plain[plain.index("--name") + 1] == "master-a1"
    opened = []
    monkeypatch.setattr(init_agent.herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(init_agent.herdr_host, "run", lambda *_: None)
    monkeypatch.setattr(init_agent.herdr_panes, "record", lambda *_: None)
    monkeypatch.setattr(
        init_agent.herdr_host,
        "open_pane",
        lambda directory, title, *_: opened.append(title) or SimpleNamespace(workspace_id="w", tab_id="t", pane_id="p"),
    )
    for title in ("master 2 · rig", ""):
        args = SimpleNamespace(title=title, placement="tab", workspace="")
        init_agent._start_herdr(tmp_path / "launch.sh", tmp_path, "master-a1", args, "claude", {})
    assert opened == ["master 2 · rig", "master-a1"]
    assert init_agent._parser().parse_args(["--title", "master 2 · rig"]).title == "master 2 · rig"
    assert init_agent._parser().parse_args([]).title == ""


def _named(tmp_path, display, identity):
    proc = tmp_path / "proc"
    (proc / "300").mkdir(parents=True)
    (proc / "300" / "environ").write_bytes(f"AGENTIHOOKS_AGENT_NAME={identity}\0".encode() if identity else b"")
    item = terminate_agent.Process(300, 1, 300, 300, 100, "S", "claude", ("claude", "--name", display))
    return terminate_agent._name(item, proc, {300: item})


def test_a_master_shown_by_its_seat_title_keeps_its_agent_identity(tmp_path):
    assert _named(tmp_path, "master 2 · rig", "master@a1b2c3-0002") == "master@a1b2c3-0002"


def test_a_seat_title_without_an_identity_is_the_name(tmp_path):
    assert _named(tmp_path, "master 2 · rig", "") == "master 2 · rig"


def test_another_display_name_still_names_the_process(tmp_path):
    assert _named(tmp_path, "operator-pane", "master@a1b2c3-0002") == "operator-pane"
