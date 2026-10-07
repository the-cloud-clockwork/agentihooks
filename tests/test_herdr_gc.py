import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts import herdr_gc, herdr_panes
from scripts.herdr_host import Placement

MIN = 60_000
NOW = 100 * MIN
SHELL = {"foreground_processes": [{"name": "bash", "argv": ["-bash"], "pid": 9}], "shell_pid": 9}
LOGIN = {"foreground_processes": [{"name": "bash", "argv": ["/bin/bash", "-l"], "pid": 9}], "shell_pid": 9}
CLAUDE = {"foreground_processes": [{"name": "claude", "argv": ["claude"], "pid": 10}], "shell_pid": 9}
LAUNCHER = {"foreground_processes": [{"name": "bash", "argv": ["bash", "/run/x/a.sh"], "pid": 9}], "shell_pid": 9}
PROMPT = "error: select profile refused\niamroot:~\n$ "


class FakeHerdr:
    def __init__(self):
        self.panes, self.info, self.screens, self.closed = {}, {}, {}, []

    def add(self, pane_id, terminal, info=SHELL, screen=PROMPT, workspace="w1", tab=None):
        tab = tab or f"{workspace}:t-{pane_id}"
        self.panes[pane_id] = {"pane_id": pane_id, "terminal_id": terminal, "tab_id": tab, "workspace_id": workspace}
        self.info[pane_id], self.screens[pane_id] = info, screen

    def list_panes(self):
        return dict(self.panes)

    def process_info(self, pane_id):
        return self.info[pane_id]

    def screen(self, pane_id):
        return self.screens[pane_id]

    def close_pane(self, pane_id):
        self.closed.append(("pane", pane_id))
        del self.panes[pane_id]

    def tabs(self):
        counts = {}
        for pane in self.panes.values():
            counts[pane["tab_id"]] = counts.get(pane["tab_id"], 0) + 1
        return [{"tab_id": t, "pane_count": counts.get(t, 0)} for t in self.extra_tabs | set(counts)]

    extra_tabs: set = set()

    def workspaces(self):
        live = {p["workspace_id"] for p in self.panes.values()}
        return [{"workspace_id": w, "pane_count": int(w in live)} for w in self.extra_workspaces | live]

    extra_workspaces: set = set()

    def close_tab(self, tab_id):
        self.closed.append(("tab", tab_id))
        self.extra_tabs = self.extra_tabs - {tab_id}

    def close_workspace(self, workspace_id):
        self.closed.append(("workspace", workspace_id))
        self.extra_workspaces = self.extra_workspaces - {workspace_id}


class FakeOwners:
    def __init__(self, states=None, prompts=None):
        self.states, self.prompts = states or {}, prompts or {}

    def state(self, record):
        return self.states.get(record.owner_session, herdr_gc.NO_SWARM if not record.owner_swarm else herdr_gc.LIVE)

    def prompted_at(self, record):
        return self.prompts.get(record.owner_session)


@pytest.fixture
def env(tmp_path):
    return {herdr_panes.ROOT_ENV: str(tmp_path / "panes"), "XDG_RUNTIME_DIR": str(tmp_path / "run")}


def launch(env, herdr, pane, terminal, *, name="eng-1", swarm="", route="", at=0, seen=None, **pane_args):
    herdr.add(pane, terminal, **pane_args)
    spot = herdr.panes[pane]
    made = herdr_panes.record(
        Placement(spot["workspace_id"], spot["tab_id"], pane, terminal),
        "init-agent",
        name,
        {**env, **({"AGENTIHOOKS_SWARM": swarm} if swarm else {})},
        at,
    )
    changes = {"route_status": route} if route else {}
    if seen is not False:
        screen = herdr.screens[pane] if seen is None else seen
        changes |= {"seen": herdr_gc.digest(screen), "active_at": at}
    return herdr_panes.update(made, env, **changes) if changes else made


def sweep(env, herdr, owners=None, act=True, now=NOW):
    return herdr_gc.sweep(env, now, act, herdr, owners or FakeOwners())


def actions(findings):
    return {f.record.pane_id: (f.action, f.reason) for f in findings if isinstance(f, herdr_gc.Finding)}


def test_an_operator_pane_is_never_touched(env):
    herdr = FakeHerdr()
    herdr.add("w1:p1", "term_op")
    launch(env, herdr, "w1:p2", "term_a")
    sweep(env, herdr)
    assert "w1:p1" in herdr.panes
    assert ("pane", "w1:p1") not in herdr.closed


def test_a_failed_launch_left_at_a_bare_shell_closes_after_the_grace(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    found = actions(sweep(env, herdr))
    assert found["w1:p2"] == (herdr_gc.CLOSE, "a failed launch left it at a bare shell")
    assert ("pane", "w1:p2") in herdr.closed
    assert herdr_panes.load(env) == []


def test_an_exited_agent_closes_with_its_own_reason(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", route="routed", info=LOGIN)
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.CLOSE, "its agent exited and left a bare shell")


def test_a_pane_inside_the_launch_grace_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", at=NOW - 4 * MIN)
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "inside the launch grace")
    assert herdr.closed == []


def test_the_grace_comes_from_the_environment(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", at=NOW - 20 * MIN)
    found = herdr_gc.sweep({**env, herdr_gc.GRACE_ENV: "30"}, NOW, True, herdr, FakeOwners())
    assert actions(found)["w1:p2"][0] == herdr_gc.KEEP


def test_a_pane_with_typed_shell_input_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", screen="iamroot:~\n$ git status")
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "its input line holds text")
    assert herdr.closed == []


def test_a_retired_agent_with_typed_input_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", swarm="crew", route="routed", info=CLAUDE, screen="─────\n❯ keep this\n─────")
    owners = FakeOwners({"eng-1": herdr_gc.RETIRED})
    assert actions(sweep(env, herdr, owners))["w1:p2"] == (herdr_gc.KEEP, "its input line holds text")


def test_stale_agent_input_on_a_bare_shell_does_not_hold_it(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", route="routed", screen="─────\n❯ old words\n─────\n$ ")
    assert actions(sweep(env, herdr))["w1:p2"][0] == herdr_gc.CLOSE


def test_a_screen_changed_since_the_last_sweep_is_kept_for_the_quiet_window(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", seen="older screen")
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "used inside the quiet window")
    assert herdr_panes.load(env)[0].active_at == NOW
    later = NOW + herdr_gc.wake.DEFAULT_QUIET_S * 1000
    assert actions(sweep(env, herdr, now=later))["w1:p2"][0] == herdr_gc.CLOSE


def test_a_first_look_counts_as_use(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", seen=False)
    assert actions(sweep(env, herdr))["w1:p2"][0] == herdr_gc.KEEP


def test_an_agent_the_operator_prompted_inside_the_quiet_window_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", swarm="crew", route="routed", info=CLAUDE)
    owners = FakeOwners({"eng-1": herdr_gc.RETIRED}, {"eng-1": NOW - MIN})
    assert actions(sweep(env, herdr, owners))["w1:p2"] == (herdr_gc.KEEP, "used inside the quiet window")


@pytest.mark.parametrize(
    "state, reason",
    [
        (herdr_gc.RETIRED, "its agent was retired"),
        (herdr_gc.STOPPED, "its swarm stopped"),
        (herdr_gc.REMOVED, "its swarm was removed"),
    ],
)
def test_a_swarm_agent_closes_once_its_swarm_lets_it_go(env, state, reason):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", swarm="crew", route="routed", info=CLAUDE)
    assert actions(sweep(env, herdr, FakeOwners({"eng-1": state})))["w1:p2"] == (herdr_gc.CLOSE, reason)


@pytest.mark.parametrize("state", [herdr_gc.LIVE, herdr_gc.UNKNOWN])
def test_a_running_agent_whose_swarm_holds_it_or_cannot_say_is_kept(env, state):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", swarm="crew", route="routed", info=CLAUDE)
    assert actions(sweep(env, herdr, FakeOwners({"eng-1": state})))["w1:p2"] == (herdr_gc.KEEP, "its agent is live")


@pytest.mark.parametrize("info", [CLAUDE, LAUNCHER, {"foreground_processes": []}])
def test_a_pane_running_anything_but_a_bare_shell_is_live(env, info):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", info=info)
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "its agent is live")


def test_a_pane_id_now_holding_another_terminal_is_forgotten_never_closed(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    herdr.panes["w1:p2"]["terminal_id"] = "term_operator"
    assert actions(sweep(env, herdr))["w1:p2"][0] == herdr_gc.FORGET
    assert herdr.closed == [] and herdr_panes.load(env) == []


def test_a_pane_herdr_no_longer_lists_is_forgotten(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    del herdr.panes["w1:p2"]
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.FORGET, "herdr no longer lists it")
    assert herdr_panes.load(env) == []


def test_without_enforce_the_sweep_only_lists(env):
    herdr = FakeHerdr()
    made = launch(env, herdr, "w1:p2", "term_a")
    found = sweep(env, herdr, act=False)
    assert actions(found)["w1:p2"][0] == herdr_gc.CLOSE
    assert herdr.closed == [] and herdr_panes.load(env) == [made]
    assert herdr_gc.lines(found, act=False) == [
        "would close pane w1:p2 of eng-1: a failed launch left it at a bare shell"
    ]


def test_every_close_is_reported_with_its_reason(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    launch(env, herdr, "w1:p3", "term_b", at=NOW)
    assert herdr_gc.lines(sweep(env, herdr), act=True) == [
        "closed pane w1:p2 of eng-1: a failed launch left it at a bare shell"
    ]


def test_a_tab_and_space_left_empty_are_closed(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w7:p1", "term_a", workspace="w7", tab="w7:t1")
    herdr.extra_tabs, herdr.extra_workspaces = {"w7:t1", "w1:t9"}, {"w7", "w1"}
    found = sweep(env, herdr)
    assert ("tab", "w7:t1") in herdr.closed and ("workspace", "w7") in herdr.closed
    assert ("tab", "w1:t9") not in herdr.closed and ("workspace", "w1") not in herdr.closed
    assert "closed workspace w7, left empty" in herdr_gc.lines(found, act=True)


def test_a_stopped_proof_space_goes_but_an_operator_pane_keeps_it(env):
    herdr = FakeHerdr()
    owners = FakeOwners({"master@p": herdr_gc.STOPPED, "eng@p": herdr_gc.STOPPED})
    for pane, name in (("w8:p1", "master@p"), ("w8:p2", "eng@p")):
        launch(env, herdr, pane, f"term-{pane}", name=name, swarm="proof-abc123-x-1", workspace="w8", info=CLAUDE)
    launch(env, herdr, "w9:p1", "term_c", name="master@p", swarm="proof-abc123-x-1", workspace="w9", info=CLAUDE)
    herdr.add("w9:p2", "term_operator", workspace="w9")
    sweep(env, herdr, owners)
    assert not any(p["workspace_id"] == "w8" for p in herdr.panes.values())
    assert set(herdr.panes) == {"w9:p2"}
    assert ("workspace", "w9") not in herdr.closed


def test_a_herdr_failure_on_one_pane_leaves_the_rest_to_run(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    launch(env, herdr, "w1:p3", "term_b")

    def broken(pane_id):
        raise herdr_gc.HerdrError("socket gone")

    herdr.process_info = lambda pane_id: broken(pane_id) if pane_id == "w1:p2" else SHELL
    found = actions(sweep(env, herdr))
    assert found["w1:p2"] == (herdr_gc.KEEP, "herdr could not read it: socket gone")
    assert found["w1:p3"][0] == herdr_gc.CLOSE


class FakeStore:
    def __init__(self, slugs, state="running", agents=()):
        self._slugs, self._state, self._agents = slugs, state, agents

    def slugs(self):
        return self._slugs

    def config(self, slug):
        return SimpleNamespace(state=self._state)

    def agents(self, slug):
        return [SimpleNamespace(name=n, state=s) for n, s in self._agents]


def owned(store_url=""):
    made = herdr_panes.PaneRecord("w1:p2", "term_a", "w1:t1", "w1", "init-agent", "eng-1", 0, "crew")
    return replace(made, swarm_store=store_url)


@pytest.mark.parametrize(
    "store, state",
    [
        (FakeStore(["crew"], agents=[("eng-1", "working")]), herdr_gc.LIVE),
        (FakeStore(["crew"], agents=[("eng-1", "finished")]), herdr_gc.RETIRED),
        (FakeStore(["crew"], agents=[("eng-2", "working")]), herdr_gc.RETIRED),
        (FakeStore(["crew"], state="stopped", agents=[("eng-1", "working")]), herdr_gc.STOPPED),
        (FakeStore(["other"]), herdr_gc.REMOVED),
    ],
)
def test_the_swarm_store_says_what_became_of_the_agent(store, state):
    assert herdr_gc.Owners(lambda url: store).state(owned()) == state


def test_an_unreachable_swarm_store_is_unknown_and_each_store_is_opened_once():
    opened = []

    def connect(url):
        opened.append(url)
        raise OSError("refused")

    owners = herdr_gc.Owners(connect)
    assert owners.state(owned("redis://localhost/3")) == herdr_gc.UNKNOWN
    assert owners.state(owned("redis://localhost/3")) == herdr_gc.UNKNOWN
    assert opened == ["redis://localhost/3"]


def test_a_withheld_swarm_store_is_unknown_and_never_opened():
    owners = herdr_gc.Owners(lambda url: pytest.fail("store opened"))
    assert owners.state(owned(herdr_panes.WITHHELD)) == herdr_gc.UNKNOWN
    assert owners.prompted_at(owned(herdr_panes.WITHHELD)) is None


def test_a_pane_outside_a_swarm_has_no_swarm_state():
    assert herdr_gc.Owners(lambda url: pytest.fail("store opened")).state(replace(owned(), owner_swarm="")) == (
        herdr_gc.NO_SWARM
    )


def test_the_last_prompt_comes_from_the_agents_swarm(monkeypatch):
    store = FakeStore(["crew"])
    store.redis = object()
    seen = []
    monkeypatch.setattr(herdr_gc.idle, "last_prompt", lambda redis, slug, name: seen.append((slug, name)) or 42)
    owners = herdr_gc.Owners(lambda url: store)
    assert owners.prompted_at(owned()) == 42 and seen == [("crew", "eng-1")]
    assert owners.prompted_at(replace(owned(), owner_swarm="")) is None


def aged(path, seconds):
    path.write_text("x", encoding="utf-8")
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))
    return path


def test_launch_files_older_than_a_day_are_removed(tmp_path):
    day = herdr_gc.RUN_FILE_AGE_S
    old = [aged(tmp_path / f"eng-1-a.{suffix}", day + 60) for suffix in ("sh", "route", "prompt", "started")]
    old.append(aged(tmp_path / "profile-12-34.json", day + 60))
    old.append(aged(tmp_path / "closing-999999", day + 60))
    fresh = aged(tmp_path / "eng-2-b.sh", 60)
    held = aged(tmp_path / "closing-77", day + 60)
    other = aged(tmp_path / "notes.txt", day + 60)
    alive = {77}.__contains__
    now = held.stat().st_mtime + day + 60
    assert sorted(herdr_gc.stale_launch_files(tmp_path, now, alive)) == sorted(old)
    assert herdr_gc.clean_run_dir(tmp_path, now, False, alive) == [f"would remove {p.name}" for p in sorted(old)]
    assert all(p.exists() for p in old)
    assert herdr_gc.clean_run_dir(tmp_path, now, True, alive) == [f"removed {p.name}" for p in sorted(old)]
    assert not any(p.exists() for p in old) and fresh.exists() and held.exists() and other.exists()


def test_a_missing_run_folder_has_nothing_to_remove(tmp_path):
    assert herdr_gc.stale_launch_files(tmp_path / "absent", 0, lambda pid: False) == []


def test_run_skips_quietly_without_a_herdr_server(env, monkeypatch):
    monkeypatch.setattr(herdr_gc.herdr_host, "server_running", lambda environ: False)
    assert herdr_gc.run(env, NOW, True) == []


def test_run_cleans_the_launch_folder_and_reports_the_sweep(env, monkeypatch):
    folder = herdr_panes.run_folder(env)
    folder.mkdir(parents=True)
    old = aged(folder / "eng-1-a.sh", herdr_gc.RUN_FILE_AGE_S + 60)
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_gc.herdr_host, "server_running", lambda environ: True)
    monkeypatch.setattr(herdr_gc, "Herdr", lambda environ: herdr)
    monkeypatch.setattr(herdr_gc, "Owners", FakeOwners)
    now = int((old.stat().st_mtime + herdr_gc.RUN_FILE_AGE_S + 60) * 1000)
    assert herdr_gc.run(env, now, False) == [
        "would remove eng-1-a.sh",
        "would close pane w1:p2 of eng-1: a failed launch left it at a bare shell",
    ]
    assert herdr_gc.run(env, now, True) == [
        "removed eng-1-a.sh",
        "closed pane w1:p2 of eng-1: a failed launch left it at a bare shell",
    ]
    assert not old.exists() and herdr.panes == {}


def test_a_herdr_failure_mid_sweep_is_reported(env, monkeypatch):
    class Down(FakeHerdr):
        def list_panes(self):
            raise herdr_gc.HerdrError("socket gone")

    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_gc.herdr_host, "server_running", lambda environ: True)
    monkeypatch.setattr(herdr_gc, "Herdr", lambda environ: Down())
    assert herdr_gc.run(env, NOW, True) == ["herdr sweep failed: socket gone"]


def test_gc_lists_without_enforce_and_closes_with_it(monkeypatch, capsys):
    from scripts import gc_cli

    acts = []
    monkeypatch.setattr(gc_cli, "sweep", lambda scope, act: {"skipped": "lifecycle off"})
    monkeypatch.setattr(herdr_gc, "run", lambda environ, now_ms, act: acts.append(act) or [f"act={act}"])
    assert gc_cli.main(["gc"]) == 0 and gc_cli.main(["gc", "--enforce"]) == 0
    out = capsys.readouterr().out
    assert acts == [False, True]
    assert "herdr: act=False" in out and "herdr: act=True" in out


def test_gc_json_carries_the_herdr_lines(monkeypatch, capsys):
    import json

    from scripts import gc_cli

    monkeypatch.setattr(gc_cli, "sweep", lambda scope, act: {"skipped": "lifecycle off"})
    monkeypatch.setattr(herdr_gc, "run", lambda environ, now_ms, act: ["would close pane w1:p2 of a: gone"])
    gc_cli.main(["gc", "--json"])
    assert json.loads(capsys.readouterr().out)["herdr"] == ["would close pane w1:p2 of a: gone"]


def test_every_tick_closes_and_journals_with_reasons(monkeypatch, capsys):
    from scripts.swarm import cli

    acts = []
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(
        herdr_gc, "run", lambda environ, now_ms, act: acts.append(act) or ["closed pane w1:p2 of a: why"]
    )
    cli.cmd_tick(SimpleNamespace(slugs=lambda: []), None)
    assert acts == [True]
    assert "herdr: closed pane w1:p2 of a: why" in capsys.readouterr().out
