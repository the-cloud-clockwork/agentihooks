import hashlib
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from scripts import herdr_host, herdr_panes, init_agent, run_in_terminal
from scripts.herdr_host import Placement

PLACED = Placement("w1", "w1:t2", "w1:p3", "term_a")


def test_a_launch_is_recorded_with_its_pane_terminal_owner_and_time(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path), "AGENTIHOOKS_SWARM": "crew"}
    made = herdr_panes.record(PLACED, "init-agent", "eng-1", env, 1000)
    assert herdr_panes.load(env) == [made]
    assert (made.pane_id, made.terminal_id, made.tab_id, made.workspace_id) == ("w1:p3", "term_a", "w1:t2", "w1")
    assert (made.kind, made.owner_session, made.owner_swarm, made.launched_at) == ("init-agent", "eng-1", "crew", 1000)


def test_a_launch_outside_a_swarm_has_no_owner_swarm(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path)}
    assert herdr_panes.record(PLACED, "run-in-terminal", "", env, 5).owner_swarm == ""


def test_the_swarm_store_is_kept_only_without_a_password_and_marked_when_withheld(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path), "AGENTIHOOKS_SWARM": "p"}
    plain = {**env, herdr_panes.STORE_ENV: "redis://localhost:6379/7"}
    secret = {**env, herdr_panes.STORE_ENV: "redis://user:pw@localhost:6379/7"}
    assert herdr_panes.record(PLACED, "init-agent", "a", plain, 1).swarm_store == "redis://localhost:6379/7"
    assert herdr_panes.record(PLACED, "init-agent", "a", secret, 1).swarm_store == herdr_panes.WITHHELD
    assert herdr_panes.record(PLACED, "init-agent", "a", env, 1).swarm_store == ""


def test_update_and_forget_rewrite_the_one_record(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path)}
    made = herdr_panes.record(PLACED, "init-agent", "a", env, 1)
    other = herdr_panes.record(Placement("w1", "w1:t3", "w1:p4", "term_b"), "init-agent", "b", env, 2)
    routed = herdr_panes.update(made, env, route_status="routed")
    assert sorted(herdr_panes.load(env), key=lambda r: r.pane_id) == [routed, other]
    herdr_panes.forget(routed, env)
    assert herdr_panes.load(env) == [other]


def test_an_unreadable_record_is_skipped(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path)}
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "partial.json").write_text('{"pane_id": "w1:p1"}', encoding="utf-8")
    assert herdr_panes.load(env) == []


def test_the_default_folder_sits_under_the_home():
    assert herdr_panes.root({}) == Path.home() / ".agentihooks" / "herdr" / "panes"


def test_the_herdr_server_is_the_socket_herdr_resolves():
    assert herdr_host.server_socket({"HERDR_SOCKET_PATH": "/s/a.sock"}) == "/s/a.sock"
    assert herdr_host.server_socket({}) == str(Path.home() / ".config" / "herdr" / "herdr.sock")


def test_records_load_only_on_the_herdr_server_they_were_opened_on(tmp_path):
    first = {herdr_panes.ROOT_ENV: str(tmp_path), "HERDR_SOCKET_PATH": "/s/a.sock"}
    second = {herdr_panes.ROOT_ENV: str(tmp_path), "HERDR_SOCKET_PATH": "/s/b.sock"}
    made = herdr_panes.record(PLACED, "init-agent", "a", first, 1)
    other = herdr_panes.record(Placement("w1", "w1:t3", "w1:p3", "term_b"), "init-agent", "b", second, 2)
    assert made.herdr_server == "/s/a.sock"
    assert herdr_panes.load(first) == [made]
    assert herdr_panes.load(second) == [other]
    herdr_panes.mark("w1:p3", first, route_status="routed")
    assert herdr_panes.load(second) == [other]


def test_a_record_without_a_herdr_server_belongs_to_the_default_server(tmp_path):
    folder = tmp_path / "panes"
    env = {herdr_panes.ROOT_ENV: str(folder)}
    legacy = replace(herdr_panes.record(PLACED, "init-agent", "a", env, 1), herdr_server="")
    for path in folder.iterdir():
        path.unlink()
    herdr_panes.update(legacy, env)
    assert [path.name for path in folder.iterdir()] == ["term_a.json"]
    assert herdr_panes.load(env) == [legacy]
    assert herdr_panes.load({**env, "HERDR_SOCKET_PATH": "/s/a.sock"}) == []
    herdr_panes.forget(legacy, env)
    assert list(folder.iterdir()) == []


def test_two_servers_recording_the_same_pane_keep_both_records(tmp_path):
    first = {herdr_panes.ROOT_ENV: str(tmp_path), "HERDR_SOCKET_PATH": "/s/a.sock"}
    second = {herdr_panes.ROOT_ENV: str(tmp_path), "HERDR_SOCKET_PATH": "/s/b.sock"}
    bare = Placement("w1", "w1:t2", "w1:p3")
    made = herdr_panes.record(bare, "init-agent", "a", first, 1)
    other = herdr_panes.record(bare, "init-agent", "b", second, 2)
    assert herdr_panes.load(first) == [made]
    assert herdr_panes.load(second) == [other]
    herdr_panes.forget(other, second)
    assert herdr_panes.load(first) == [made]
    assert herdr_panes.load(second) == []


def test_a_created_pane_carries_its_terminal_id(tmp_path, monkeypatch):
    pane = {"workspace_id": "w9", "tab_id": "w9:t2", "pane_id": "w9:p4", "terminal_id": "term_9"}
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {"root_pane": pane})
    placed = herdr_host.open_pane(tmp_path, "eng-a", {}, "tab", "", {"HERDR_WORKSPACE_ID": "w5"})
    assert placed == Placement("w9", "w9:t2", "w9:p4", "term_9")


def _launch(monkeypatch, tmp_path, *extra, **env_extra):
    monkeypatch.setattr(init_agent.agent_choice, "choose", lambda requested, environ: ("claude", "rotation"))
    monkeypatch.setattr(herdr_host, "open_pane", lambda *a: Placement("w1", "w1:t3", "w1:p7", "term_7"))
    monkeypatch.setattr(herdr_host, "run", lambda pane, launcher, environ: launcher.with_suffix(".started").touch())
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        init_agent,
        "_launch_command",
        lambda launcher, directory, title, environ: ("linux", ["/usr/bin/terminal", str(launcher)]),
    )
    monkeypatch.setattr(
        init_agent.subprocess, "Popen", lambda command, **k: Path(command[-1]).with_suffix(".started").touch()
    )
    env = {
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": str(tmp_path / "rt"),
        "AGENTIHOOKS_HOME": str(tmp_path),
        herdr_panes.ROOT_ENV: str(tmp_path / "panes"),
        **env_extra,
    }
    args = ["--dir", str(tmp_path), "--name", "eng-a", "--start-timeout", "0.5", "--route-timeout", "0", *extra]
    assert init_agent.main(args, env) == 0
    return herdr_panes.load(env)


def test_a_herdr_launch_is_recorded_with_its_terminal_owner_and_route(monkeypatch, tmp_path):
    [made] = _launch(monkeypatch, tmp_path)
    assert (made.pane_id, made.terminal_id, made.tab_id, made.workspace_id) == ("w1:p7", "term_7", "w1:t3", "w1")
    assert (made.kind, made.owner_session, made.owner_swarm, made.route_status) == (
        "init-agent",
        "eng-a",
        "",
        "pending",
    )
    assert made.launched_at > 0


def test_a_swarm_spawn_records_its_swarm(monkeypatch, tmp_path):
    [made] = _launch(monkeypatch, tmp_path, AGENTIHOOKS_SWARM_SPAWN="1", AGENTIHOOKS_SWARM="crew")
    assert made.owner_swarm == "crew"


def test_a_session_inside_a_swarm_launching_by_hand_records_no_swarm(monkeypatch, tmp_path):
    [made] = _launch(monkeypatch, tmp_path, AGENTIHOOKS_SWARM="crew", AGENTIHOOKS_AGENT_NAME="me")
    assert made.owner_swarm == ""


def test_a_native_launch_records_no_pane(monkeypatch, tmp_path):
    assert _launch(monkeypatch, tmp_path, "--host", "native") == []


def _terminal(monkeypatch):
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(herdr_host, "open_pane", lambda *a: Placement("w1", "w1:t2", "w1:p3", "term_3"))
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {})


def test_a_command_tab_is_recorded_for_the_sweep(monkeypatch, tmp_path):
    _terminal(monkeypatch)
    env = {"HOME": str(tmp_path), herdr_panes.ROOT_ENV: str(tmp_path / "panes"), "AGENTIHOOKS_AGENT_NAME": "eng-1"}
    assert run_in_terminal.main(["--dir", str(tmp_path), "--", "npm", "test"], env) == 0
    [made] = herdr_panes.load(env)
    assert (made.pane_id, made.terminal_id, made.kind, made.owner_session) == (
        "w1:p3",
        "term_3",
        "run-in-terminal",
        "eng-1",
    )


def test_a_plain_shell_tab_is_the_operators_and_is_not_recorded(monkeypatch, tmp_path):
    _terminal(monkeypatch)
    env = {"HOME": str(tmp_path), herdr_panes.ROOT_ENV: str(tmp_path / "panes")}
    assert run_in_terminal.main(["--dir", str(tmp_path)], env) == 0
    assert herdr_panes.load(env) == []


def test_a_record_lands_in_the_configured_folder_named_for_its_terminal(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path / "a" / "b"), "HERDR_SOCKET_PATH": "/s/a.sock"}
    herdr_panes.record(Placement("w1", "w1:t2", "w1:p3", "term:A/b"), "init-agent", "a", env, 1)
    herdr_panes.record(Placement("w1", "w1:t2", "w1:p4"), "init-agent", "b", env, 1)
    server = hashlib.sha256(b"/s/a.sock").hexdigest()[:12]
    names = sorted(path.name for path in (tmp_path / "a" / "b").iterdir())
    assert names == [f"{server}_term_A_b.json", f"{server}_w1_p4.json"]


def test_a_store_url_outside_a_swarm_is_not_kept(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path), herdr_panes.STORE_ENV: "redis://localhost:6379/7"}
    assert herdr_panes.record(PLACED, "run-in-terminal", "", env, 5).swarm_store == ""


def test_forgetting_twice_is_harmless(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path)}
    made = herdr_panes.record(PLACED, "init-agent", "a", env, 1)
    herdr_panes.forget(made, env)
    herdr_panes.forget(made, env)
    assert herdr_panes.load(env) == []


def test_mark_updates_only_the_named_pane(tmp_path):
    env = {herdr_panes.ROOT_ENV: str(tmp_path)}
    made = herdr_panes.record(PLACED, "init-agent", "a", env, 1)
    other = herdr_panes.record(Placement("w1", "w1:t3", "w1:p4", "term_b"), "init-agent", "b", env, 2)
    herdr_panes.mark("w1:p3", env, route_status="routed")
    herdr_panes.mark("", env, route_status="bare")
    loaded = {r.pane_id: r for r in herdr_panes.load(env)}
    assert loaded == {"w1:p3": replace(made, route_status="routed"), "w1:p4": other}


def test_the_launch_run_folder_sits_in_the_runtime_dir(tmp_path):
    assert herdr_panes.run_folder({"XDG_RUNTIME_DIR": str(tmp_path)}) == tmp_path / "agentihooks-claude-terminal"
    assert herdr_panes.run_folder({}) == Path(tempfile.gettempdir()) / "agentihooks-claude-terminal"


def test_a_routed_launch_records_its_route_and_launch_time(monkeypatch, tmp_path):
    def started(pane, launcher, environ):
        launcher.with_suffix(".route").write_text("status=routed\naccount=a\n", encoding="utf-8")
        launcher.with_suffix(".started").touch()

    before = time.time() * 1000
    monkeypatch.setattr(herdr_host, "rename_agent", lambda pane, name, environ: True)
    monkeypatch.setattr(herdr_host, "run", started)
    monkeypatch.setattr(herdr_host, "open_pane", lambda *a: Placement("w1", "w1:t3", "w1:p7", "term_7"))
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "ensure_server", lambda environ: False)
    monkeypatch.setattr(init_agent.shutil, "which", lambda name: None)
    env = {"HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path / "rt"), herdr_panes.ROOT_ENV: str(tmp_path / "p")}
    args = ["--dir", str(tmp_path), "--name", "eng-a", "--start-timeout", "5", "--route-timeout", "5"]
    assert init_agent.main(args, env) == 0
    [made] = herdr_panes.load(env)
    assert made.route_status == "routed"
    assert before - 1 <= made.launched_at <= time.time() * 1000


def test_a_command_tab_without_a_calling_agent_records_its_launch_time(monkeypatch, tmp_path):
    _terminal(monkeypatch)
    env = {"HOME": str(tmp_path), herdr_panes.ROOT_ENV: str(tmp_path / "panes")}
    before = time.time() * 1000
    assert run_in_terminal.main(["--dir", str(tmp_path), "--", "make"], env) == 0
    [made] = herdr_panes.load(env)
    assert made.owner_session == ""
    assert before - 1 <= made.launched_at <= time.time() * 1000
