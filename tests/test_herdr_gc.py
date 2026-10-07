import hashlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

import pytest

from scripts import herdr_gc, herdr_panes
from scripts.herdr_host import Placement

MIN = 60_000
NOW = 100 * MIN
QUIET = herdr_gc.wake.DEFAULT_QUIET_S * 1000
SHELL = {"foreground_processes": [{"name": "bash", "argv": ["-bash"], "pid": 9}], "shell_pid": 9}
LOGIN = {"foreground_processes": [{"name": "bash", "argv": ["/bin/bash", "-l"], "pid": 9}], "shell_pid": 9}
CLAUDE = {"foreground_processes": [{"name": "claude", "argv": ["claude"], "pid": 10}], "shell_pid": 9}
LAUNCHER = {"foreground_processes": [{"name": "bash", "argv": ["bash", "/run/x/a.sh"], "pid": 9}], "shell_pid": 9}
PROMPT = "error: select profile refused\niamroot:~\n$ "


@dataclass(frozen=True)
class Spot:
    info: dict = field(default_factory=lambda: SHELL)
    screen: str = PROMPT
    workspace: str = "w1"
    tab: str = ""


@dataclass(frozen=True)
class Owner:
    name: str = "eng-1"
    swarm: str = ""
    route: str = ""
    at: int = 0
    seen: str | None = None
    kind: str = "init-agent"


class FakeHerdr:
    def __init__(self):
        self.panes, self.info, self.screens, self.closed = {}, {}, {}, []
        self.extra_tabs, self.extra_workspaces = set(), set()

    def add(self, pane_id, terminal, spot=Spot()):
        tab = spot.tab or f"{spot.workspace}:t-{pane_id}"
        self.panes[pane_id] = {
            "pane_id": pane_id,
            "terminal_id": terminal,
            "tab_id": tab,
            "workspace_id": spot.workspace,
        }
        self.info[pane_id], self.screens[pane_id] = spot.info, spot.screen

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
        counts = {tab: 0 for tab in self.extra_tabs}
        for pane in self.panes.values():
            counts[pane["tab_id"]] = counts.get(pane["tab_id"], 0) + 1
        return [{"tab_id": tab, "pane_count": count} for tab, count in counts.items()]

    def workspaces(self):
        counts = {space: 0 for space in self.extra_workspaces}
        for pane in self.panes.values():
            counts[pane["workspace_id"]] = counts.get(pane["workspace_id"], 0) + 1
        return [{"workspace_id": space, "pane_count": count} for space, count in counts.items()]

    def close_tab(self, tab_id):
        self.closed.append(("tab", tab_id))
        self.extra_tabs.discard(tab_id)

    def close_workspace(self, workspace_id):
        self.closed.append(("workspace", workspace_id))
        self.extra_workspaces.discard(workspace_id)


class FakeOwners:
    def __init__(self, states=None, prompts=None):
        self.states, self.prompts = states or {}, prompts or {}

    def state(self, record):
        default = herdr_gc.LIVE if record.owner_swarm else herdr_gc.NO_SWARM
        return self.states.get(record.owner_session, default)

    def prompted_at(self, record):
        return self.prompts.get(record.owner_session)


@pytest.fixture
def env(tmp_path):
    return {herdr_panes.ROOT_ENV: str(tmp_path / "panes"), "XDG_RUNTIME_DIR": str(tmp_path / "run")}


def launch(env, herdr, pane_id, terminal, spot=Spot(), owner=Owner()):
    herdr.add(pane_id, terminal, spot)
    where = herdr.panes[pane_id]
    placed = Placement(where["workspace_id"], where["tab_id"], pane_id, terminal)
    made = herdr_panes.record(placed, owner.kind, owner.name, {**env, "AGENTIHOOKS_SWARM": owner.swarm}, owner.at)
    changes = {"route_status": owner.route} if owner.route else {}
    if owner.seen != "":
        screen = herdr.screens[pane_id] if owner.seen is None else owner.seen
        changes |= {"seen": herdr_gc.digest(screen), "active_at": owner.at}
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
    launch(env, herdr, "w1:p2", "term_a", Spot(info=LOGIN), Owner(route="routed"))
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.CLOSE, "its agent exited and left a bare shell")


def test_an_exited_terminal_command_closes_with_its_own_reason(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", owner=Owner(kind="run-in-terminal"))
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.CLOSE, "its command exited and left a bare shell")


def test_a_pane_inside_the_launch_grace_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", owner=Owner(at=NOW - 4 * MIN))
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "inside the launch grace")
    assert herdr.closed == []


def test_the_grace_ends_exactly_at_its_length(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", owner=Owner(at=NOW - herdr_gc.DEFAULT_GRACE_MINUTES * MIN))
    assert actions(sweep(env, herdr))["w1:p2"][0] == herdr_gc.CLOSE


def test_the_grace_comes_from_the_environment(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", owner=Owner(at=NOW - 20 * MIN))
    found = herdr_gc.sweep({**env, herdr_gc.GRACE_ENV: "30"}, NOW, True, herdr, FakeOwners())
    assert actions(found)["w1:p2"][0] == herdr_gc.KEEP


def test_a_pane_with_typed_shell_input_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", Spot(screen="iamroot:~\n$ git status"))
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "its input line holds text")
    assert herdr.closed == []


def test_a_retired_agent_with_typed_input_is_kept(env):
    herdr = FakeHerdr()
    spot = Spot(info=CLAUDE, screen="─────\n❯ keep this\n─────")
    launch(env, herdr, "w1:p2", "term_a", spot, Owner(swarm="crew", route="routed"))
    owners = FakeOwners({"eng-1": herdr_gc.RETIRED})
    assert actions(sweep(env, herdr, owners))["w1:p2"] == (herdr_gc.KEEP, "its input line holds text")


def test_only_the_last_shell_line_is_the_input_line(env):
    herdr = FakeHerdr()
    spot = Spot(screen="iamroot:~\n$ git status\nclean\n❯ old words\n$ ")
    launch(env, herdr, "w1:p2", "term_a", spot, Owner(route="routed"))
    assert actions(sweep(env, herdr))["w1:p2"][0] == herdr_gc.CLOSE


def test_a_screen_changed_since_the_last_sweep_is_kept_for_the_quiet_window(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", owner=Owner(seen="older screen"))
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "used inside the quiet window")
    assert herdr_panes.load(env)[0].active_at == NOW
    assert actions(sweep(env, herdr, now=NOW + QUIET))["w1:p2"][0] == herdr_gc.CLOSE


def test_the_quiet_window_ends_exactly_at_its_length(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    found = herdr_gc.sweep({**env, herdr_gc.GRACE_ENV: "0"}, QUIET, True, herdr, FakeOwners())
    assert actions(found)["w1:p2"][0] == herdr_gc.CLOSE


def test_a_first_look_counts_as_use_and_even_a_dry_run_records_it(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", owner=Owner(seen=""))
    assert actions(sweep(env, herdr, act=False))["w1:p2"][0] == herdr_gc.KEEP
    [looked] = herdr_panes.load(env)
    assert (looked.seen, looked.active_at) == (herdr_gc.digest(PROMPT), NOW)
    assert actions(sweep(env, herdr, act=False, now=NOW + QUIET))["w1:p2"][0] == herdr_gc.CLOSE
    assert herdr.closed == []


def test_an_agent_the_operator_prompted_inside_the_quiet_window_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", Spot(info=CLAUDE), Owner(swarm="crew", route="routed"))
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
    launch(env, herdr, "w1:p2", "term_a", Spot(info=CLAUDE), Owner(swarm="crew", route="routed"))
    assert actions(sweep(env, herdr, FakeOwners({"eng-1": state})))["w1:p2"] == (herdr_gc.CLOSE, reason)


def test_a_running_agent_its_swarm_holds_is_kept(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", Spot(info=CLAUDE), Owner(swarm="crew", route="routed"))
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "its agent is live")


@pytest.mark.parametrize("info", [CLAUDE, SHELL])
def test_a_pane_whose_swarm_store_cannot_be_read_is_kept(env, info):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", Spot(info=info), Owner(swarm="crew"))
    owners = FakeOwners({"eng-1": herdr_gc.UNKNOWN})
    assert actions(sweep(env, herdr, owners))["w1:p2"] == (herdr_gc.KEEP, "its swarm store cannot be read")


@pytest.mark.parametrize("info", [CLAUDE, LAUNCHER, {"foreground_processes": []}])
def test_a_pane_running_anything_but_a_bare_shell_is_live(env, info):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a", Spot(info=info))
    assert actions(sweep(env, herdr))["w1:p2"] == (herdr_gc.KEEP, "its agent is live")


def test_a_pane_id_now_holding_another_terminal_is_forgotten_never_closed(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    herdr.panes["w1:p2"]["terminal_id"] = "term_operator"
    found = actions(sweep(env, herdr))
    assert found["w1:p2"] == (herdr_gc.FORGET, "its pane id now holds another terminal")
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
    herdr.tabs = herdr.workspaces = lambda: pytest.fail("a dry run looked for emptied tabs")
    found = sweep(env, herdr, act=False)
    assert actions(found)["w1:p2"][0] == herdr_gc.CLOSE
    assert herdr.closed == [] and herdr_panes.load(env) == [made]
    assert herdr_gc.lines(found, act=False) == [
        "would close pane w1:p2 of eng-1: a failed launch left it at a bare shell"
    ]


def test_every_close_is_reported_with_its_reason(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    launch(env, herdr, "w1:p3", "term_b", owner=Owner(at=NOW))
    launch(env, herdr, "w1:p4", "term_c", owner=Owner(name=""))
    assert herdr_gc.lines(sweep(env, herdr), act=True) == [
        "closed pane w1:p2 of eng-1: a failed launch left it at a bare shell",
        "closed pane w1:p4 of no session: a failed launch left it at a bare shell",
    ]


def test_a_tab_and_space_left_empty_are_closed(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w7:p1", "term_a", Spot(workspace="w7", tab="w7:t1"))
    herdr.extra_tabs, herdr.extra_workspaces = {"w7:t1", "w1:t9"}, {"w7", "w1"}
    found = sweep(env, herdr)
    assert [c for c in herdr.closed if c[0] != "pane"] == [("tab", "w7:t1"), ("workspace", "w7")]
    assert herdr_gc.lines(found, act=True)[-2:] == ["closed tab w7:t1, left empty", "closed workspace w7, left empty"]


def test_a_tab_still_holding_an_operator_pane_stays(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w7:p1", "term_a", Spot(workspace="w7", tab="w7:t1"))
    herdr.add("w7:p2", "term_operator", Spot(workspace="w7", tab="w7:t1"))
    sweep(env, herdr)
    assert herdr.closed == [("pane", "w7:p1")]


def test_a_stopped_proof_space_goes_but_an_operator_pane_keeps_it(env):
    herdr = FakeHerdr()
    owners = FakeOwners({"master@p": herdr_gc.STOPPED, "eng@p": herdr_gc.STOPPED})
    swarm = "proof-abc123-x-1"
    for pane, name in (("w8:p1", "master@p"), ("w8:p2", "eng@p")):
        launch(env, herdr, pane, f"term-{pane}", Spot(info=CLAUDE, workspace="w8"), Owner(name=name, swarm=swarm))
    launch(env, herdr, "w9:p1", "term_c", Spot(info=CLAUDE, workspace="w9"), Owner(name="master@p", swarm=swarm))
    herdr.add("w9:p2", "term_operator", Spot(workspace="w9"))
    sweep(env, herdr, owners)
    assert set(herdr.panes) == {"w9:p2"}
    assert ("workspace", "w9") not in herdr.closed


def test_a_herdr_failure_on_one_pane_leaves_the_rest_to_run(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    launch(env, herdr, "w1:p3", "term_b")

    def info(pane_id):
        if pane_id == "w1:p2":
            raise herdr_gc.HerdrError("socket gone")
        return SHELL

    herdr.process_info = info
    found = actions(sweep(env, herdr))
    assert found["w1:p2"] == (herdr_gc.KEEP, "herdr could not read it: socket gone")
    assert found["w1:p3"][0] == herdr_gc.CLOSE


def test_a_herdr_failure_closing_emptied_tabs_keeps_the_pane_closes(env):
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")

    def down():
        raise herdr_gc.HerdrError("socket gone")

    herdr.tabs = down
    assert actions(sweep(env, herdr))["w1:p2"][0] == herdr_gc.CLOSE


def test_the_screen_digest_is_the_full_sha256():
    assert herdr_gc.digest("a") == hashlib.sha256(b"a").hexdigest()


class FakeStore:
    def __init__(self, slug, state="running", agents=()):
        self.slug, self._state, self._agents = slug, state, agents
        self.redis = object()

    def slugs(self):
        return [self.slug]

    def config(self, slug):
        assert slug == self.slug
        return SimpleNamespace(state=self._state)

    def agents(self, slug):
        assert slug == self.slug
        return [SimpleNamespace(name=n, state=s) for n, s in self._agents]


def owned(store_url=""):
    made = herdr_panes.PaneRecord("w1:p2", "term_a", "w1:t1", "w1", "init-agent", "eng-1", 0, "crew")
    return replace(made, swarm_store=store_url)


@pytest.mark.parametrize(
    "store, state",
    [
        (FakeStore("crew", agents=[("eng-1", "working")]), herdr_gc.LIVE),
        (FakeStore("crew", agents=[("eng-1", "finished")]), herdr_gc.RETIRED),
        (FakeStore("crew", agents=[("eng-2", "working")]), herdr_gc.RETIRED),
        (FakeStore("crew", state="stopped", agents=[("eng-1", "working")]), herdr_gc.STOPPED),
        (FakeStore("other"), herdr_gc.REMOVED),
    ],
)
def test_the_swarm_store_says_what_became_of_the_agent(store, state):
    assert herdr_gc.Owners(lambda url: store).state(owned()) == state


def test_a_store_that_fails_mid_read_is_unknown():
    store = FakeStore("crew")

    def down():
        raise ConnectionError("gone")

    store.slugs = down
    assert herdr_gc.Owners(lambda url: store).state(owned()) == herdr_gc.UNKNOWN


def test_an_unreachable_swarm_store_is_unknown_and_each_store_is_opened_once():
    opened = []

    def connect(url):
        opened.append(url)
        raise OSError("refused")

    owners = herdr_gc.Owners(connect)
    assert owners.state(owned("redis://localhost/3")) == herdr_gc.UNKNOWN
    assert owners.state(owned("redis://localhost/3")) == herdr_gc.UNKNOWN
    assert owners.prompted_at(owned("redis://localhost/3")) is None
    assert opened == ["redis://localhost/3"]


def test_a_withheld_swarm_store_is_unknown_and_never_opened():
    owners = herdr_gc.Owners(lambda url: pytest.fail("store opened"))
    assert owners.state(owned(herdr_panes.WITHHELD)) == herdr_gc.UNKNOWN
    assert owners.prompted_at(owned(herdr_panes.WITHHELD)) is None


def test_a_pane_outside_a_swarm_has_no_swarm_state():
    owners = herdr_gc.Owners(lambda url: pytest.fail("store opened"))
    assert owners.state(replace(owned(), owner_swarm="")) == herdr_gc.NO_SWARM
    assert owners.prompted_at(replace(owned(), owner_swarm="")) is None


def test_the_last_prompt_comes_from_the_agents_swarm(monkeypatch):
    store = FakeStore("crew")
    seen = []
    monkeypatch.setattr(herdr_gc.idle, "last_prompt", lambda redis, slug, name: seen.append((redis, slug, name)) or 42)
    assert herdr_gc.Owners(lambda url: store).prompted_at(owned()) == 42
    assert seen == [(store.redis, "crew", "eng-1")]


def test_a_failing_last_prompt_read_is_none(monkeypatch):
    def broken(redis, slug, name):
        raise ConnectionError("gone")

    monkeypatch.setattr(herdr_gc.idle, "last_prompt", broken)
    assert herdr_gc.Owners(lambda url: FakeStore("crew")).prompted_at(owned()) is None


@pytest.mark.parametrize(
    "url, environ",
    [("redis://localhost:6379/7", {herdr_panes.STORE_ENV: "redis://localhost:6379/7"}), ("", None)],
)
def test_a_store_opens_on_the_recorded_url_or_the_default(monkeypatch, url, environ):
    from scripts.swarm import store

    opened = []
    monkeypatch.setattr(store, "redis_client", lambda env=None: opened.append(env) or "client")
    assert herdr_gc._connect(url).redis == "client"
    assert opened == [environ]


class FakeCli:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, args, environ):
        self.calls.append((args, environ))
        return self.replies.get(tuple(args[:2]), {})


def test_the_herdr_adapter_speaks_the_herdr_cli(monkeypatch):
    pane = {"pane_id": "w1:p2", "terminal_id": "term_a"}
    cli = FakeCli(
        {
            ("pane", "list"): {"panes": [pane]},
            ("pane", "process-info"): {"process_info": SHELL},
            ("tab", "list"): {"tabs": [{"tab_id": "w1:t1"}]},
            ("workspace", "list"): {"workspaces": [{"workspace_id": "w1"}]},
        }
    )
    monkeypatch.setattr(herdr_gc.herdr_host, "_cli", cli)
    environ = {"HERDR_SOCKET_PATH": "/s"}
    herdr = herdr_gc.Herdr(environ)
    assert herdr.list_panes() == {"w1:p2": pane}
    assert herdr.process_info("w1:p2") == SHELL
    assert herdr.tabs() == [{"tab_id": "w1:t1"}]
    assert herdr.workspaces() == [{"workspace_id": "w1"}]
    herdr.close_pane("w1:p2")
    herdr.close_tab("w1:t1")
    herdr.close_workspace("w1")
    assert [args for args, _ in cli.calls] == [
        ["pane", "list"],
        ["pane", "process-info", "--pane", "w1:p2"],
        ["tab", "list"],
        ["workspace", "list"],
        ["pane", "close", "w1:p2"],
        ["tab", "close", "w1:t1"],
        ["workspace", "close", "w1"],
    ]
    assert all(env is environ for _, env in cli.calls)


def test_the_herdr_adapter_reads_empty_replies_as_nothing(monkeypatch):
    monkeypatch.setattr(herdr_gc.herdr_host, "_cli", FakeCli({}))
    herdr = herdr_gc.Herdr({})
    assert (herdr.list_panes(), herdr.process_info("p")) == ({}, {})
    assert (herdr.tabs(), herdr.workspaces()) == ([], [])


def test_the_herdr_adapter_reads_the_screen_as_raw_text(monkeypatch):
    ran = []
    environ = {"HERDR_SOCKET_PATH": "/s"}
    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(
        herdr_gc.subprocess,
        "run",
        lambda argv, **kwargs: ran.append((argv, kwargs)) or herdr_gc.subprocess.CompletedProcess(argv, 0, PROMPT, ""),
    )
    assert herdr_gc.Herdr(environ).screen("w1:p2") == PROMPT
    [(argv, kwargs)] = ran
    assert argv == ["/bin/herdr", "pane", "read", "w1:p2", "--source", "visible", "--format", "text"]
    assert kwargs["env"] is environ and kwargs["timeout"] == 30


@pytest.mark.parametrize(
    "binary, reply, error",
    [
        ("/bin/herdr", (1, "", "no such pane\n"), "herdr pane read: no such pane"),
        (None, None, "herdr is not installed"),
    ],
)
def test_a_screen_herdr_cannot_read_is_an_error(monkeypatch, binary, reply, error):
    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: binary)
    monkeypatch.setattr(
        herdr_gc.subprocess, "run", lambda argv, **kwargs: herdr_gc.subprocess.CompletedProcess(argv, *reply)
    )
    with pytest.raises(herdr_gc.HerdrError, match=f"^{error}$"):
        herdr_gc.Herdr({}).screen("w1:p2")


@pytest.mark.parametrize(
    "info, bare",
    [
        (SHELL, True),
        (LOGIN, True),
        ({"foreground_processes": [{"name": "zsh"}]}, True),
        (LAUNCHER, False),
        (CLAUDE, False),
        ({"foreground_processes": [SHELL["foreground_processes"][0], CLAUDE["foreground_processes"][0]]}, False),
        ({}, False),
    ],
)
def test_a_bare_shell_is_only_shells_without_a_script(info, bare):
    assert herdr_gc.bare_shell(info) is bare


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
    kept = [aged(tmp_path / name, day + 60) for name in ("notes.txt", "notes.json", "closing-x")]
    (tmp_path / "old.sh").mkdir()
    (tmp_path / "gone.sh").symlink_to(tmp_path / "missing")
    alive = {77}.__contains__
    now = held.stat().st_mtime + day + 60
    assert herdr_gc.stale_launch_files(tmp_path, now, alive) == sorted(old)
    assert herdr_gc.clean_run_dir(tmp_path, now, False, alive) == [f"would remove {p.name}" for p in sorted(old)]
    assert all(p.exists() for p in old)
    assert herdr_gc.clean_run_dir(tmp_path, now, True, alive) == [f"removed {p.name}" for p in sorted(old)]
    assert not any(p.exists() for p in old)
    assert fresh.exists() and held.exists() and all(p.exists() for p in kept)


def test_a_launch_file_exactly_a_day_old_stays(tmp_path):
    path = aged(tmp_path / "eng-1-a.sh", 0)
    assert herdr_gc.stale_launch_files(tmp_path, path.stat().st_mtime + herdr_gc.RUN_FILE_AGE_S) == []


def test_a_launch_file_another_sweep_removed_first_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(herdr_gc, "stale_launch_files", lambda folder, now_s, alive: [tmp_path / "gone.sh"])
    assert herdr_gc.clean_run_dir(tmp_path, 0, True) == ["removed gone.sh"]


def test_a_missing_run_folder_has_nothing_to_remove(tmp_path):
    assert herdr_gc.stale_launch_files(tmp_path / "absent", 0, lambda pid: False) == []


def test_a_launcher_process_is_alive_until_it_exits():
    sent = []

    def gone(pid, signal):
        raise ProcessLookupError

    def foreign(pid, signal):
        raise PermissionError

    assert herdr_gc._alive(41, lambda pid, signal: sent.append((pid, signal)))
    assert sent == [(41, 0)]
    assert not herdr_gc._alive(41, gone)
    assert herdr_gc._alive(41, foreign)


def test_run_skips_the_sweep_without_a_herdr_server(env, monkeypatch):
    seen = []
    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_gc.herdr_host, "server_running", lambda environ: seen.append(environ) or False)
    assert herdr_gc.run(env, NOW, True) == []
    assert seen == [env]


def test_run_skips_the_sweep_without_herdr(env, monkeypatch):
    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: None)
    monkeypatch.setattr(herdr_gc.herdr_host, "server_running", lambda environ: pytest.fail("herdr asked"))
    assert herdr_gc.run(env, NOW, True) == []


def test_run_cleans_the_launch_folder_and_reports_the_sweep(env, monkeypatch):
    folder = herdr_panes.run_folder(env)
    folder.mkdir(parents=True)
    old = aged(folder / "eng-1-a.sh", herdr_gc.RUN_FILE_AGE_S + 60)
    fresh = aged(folder / "eng-2-b.sh", 60)
    herdr = FakeHerdr()
    launch(env, herdr, "w1:p2", "term_a")
    made = []
    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_gc.herdr_host, "server_running", lambda environ: True)
    monkeypatch.setattr(herdr_gc, "Herdr", lambda environ: made.append(environ) or herdr)
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
    assert not old.exists() and fresh.exists() and herdr.panes == {}
    assert made == [env, env]


def test_a_herdr_failure_mid_sweep_is_reported(env, monkeypatch):
    class Down(FakeHerdr):
        def list_panes(self):
            raise herdr_gc.HerdrError("socket gone")

    monkeypatch.setattr(herdr_gc.herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_gc.herdr_host, "server_running", lambda environ: True)
    monkeypatch.setattr(herdr_gc, "Herdr", lambda environ: Down())
    assert herdr_gc.run(env, NOW, True) == ["herdr sweep failed: socket gone"]


def _gc(monkeypatch, argv):
    from scripts import gc_cli

    calls = []
    monkeypatch.setattr(gc_cli, "sweep", lambda scope, act: {"skipped": "lifecycle off"})
    monkeypatch.setattr(
        herdr_gc, "run", lambda environ, now_ms, act: calls.append((environ, now_ms, act)) or [f"act={act}"]
    )
    return gc_cli.main(argv), calls


def test_gc_lists_without_enforce_and_closes_with_it(monkeypatch, capsys):
    before = time.time() * 1000
    listed, first = _gc(monkeypatch, ["gc"])
    enforced, second = _gc(monkeypatch, ["gc", "--enforce"])
    out = capsys.readouterr().out
    assert (listed, enforced) == (0, 0)
    assert [act for _, _, act in first + second] == [False, True]
    assert all(environ == dict(os.environ) and before - 1 <= now <= time.time() * 1000 for environ, now, _ in first)
    assert "herdr: act=False" in out and "herdr: act=True" in out


def test_gc_json_carries_the_herdr_lines(monkeypatch, capsys):
    code, _ = _gc(monkeypatch, ["gc", "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert out == json.dumps({"skipped": "lifecycle off", "herdr": ["act=False"]}, indent=2) + "\n"


def test_gc_sweeps_herdr_before_a_lifecycle_failure(monkeypatch, capsys):
    from scripts import gc_cli

    def broken(scope, act):
        raise PermissionError("a folder gc cannot read")

    monkeypatch.setattr(gc_cli, "sweep", broken)
    monkeypatch.setattr(herdr_gc, "run", lambda environ, now_ms, act: ["closed pane w1:p2 of a: why"])
    with pytest.raises(PermissionError):
        gc_cli.main(["gc", "--enforce"])
    assert capsys.readouterr().out == "herdr: closed pane w1:p2 of a: why\n"


def test_every_tick_closes_and_journals_with_reasons(monkeypatch, capsys):
    from scripts.swarm import cli

    calls = []
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(
        herdr_gc,
        "run",
        lambda environ, now_ms, act: calls.append((environ, now_ms, act)) or ["closed pane w1:p2 of a: why"],
    )
    cli.cmd_tick(SimpleNamespace(slugs=lambda: []), None)
    [(environ, now, act)] = calls
    assert act is True and environ == dict(os.environ) and isinstance(now, int)
    assert "herdr: closed pane w1:p2 of a: why" in capsys.readouterr().out


def test_a_failing_sweep_never_breaks_the_tick(monkeypatch, capsys):
    from scripts.swarm import cli

    def broken(environ, now_ms, act):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(herdr_gc, "run", broken)
    cli.cmd_tick(SimpleNamespace(slugs=lambda: []), None)
    captured = capsys.readouterr()
    assert captured.err == "herdr: RuntimeError: boom\n" and captured.out == ""


def test_the_suite_never_sweeps_the_real_launch_folder_or_herdr(tmp_path):
    assert herdr_panes.run_folder(dict(os.environ)).is_relative_to(tmp_path)
    assert os.environ["HERDR_SOCKET_PATH"].startswith(str(tmp_path))
