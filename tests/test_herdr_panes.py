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


def test_a_created_pane_carries_its_terminal_id(tmp_path, monkeypatch):
    pane = {"workspace_id": "w9", "tab_id": "w9:t2", "pane_id": "w9:p4", "terminal_id": "term_9"}
    monkeypatch.setattr(herdr_host, "_cli", lambda args, environ: {"root_pane": pane})
    placed = herdr_host.open_pane(tmp_path, "eng-a", {}, "tab", "", {"HERDR_WORKSPACE_ID": "w5"})
    assert placed == Placement("w9", "w9:t2", "w9:p4", "term_9")


def _launch(monkeypatch, tmp_path, *extra, **env_extra):
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
