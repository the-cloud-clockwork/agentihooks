import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm import master_fallback

pytestmark = pytest.mark.unit
MINUTE = 60_000


@dataclass
class Agent:
    name: str
    lane: str = "master"


@dataclass
class Config:
    state: str = "running"


@dataclass
class Store:
    agents_by_slug: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)

    def agents(self, slug):
        return self.agents_by_slug.get(slug, [])

    def config(self, slug):
        return Config(self.states.get(slug, "running"))


def _launches(code=0):
    started = []

    def launch(slug):
        started.append(slug)
        return code

    return started, launch


def test_the_fallback_minutes_default_to_twenty_and_follow_the_env():
    assert master_fallback.minutes({}) == 20
    assert master_fallback.minutes({master_fallback.MINUTES: ""}) == 20
    assert master_fallback.minutes({master_fallback.MINUTES: "7"}) == 7
    assert (master_fallback.MINUTES, master_fallback.SWARMS) == (
        "AGENTIHOOKS_MASTER_FALLBACK_MINUTES",
        "AGENTIHOOKS_MASTER_FALLBACK_SWARMS",
    )


def test_the_watched_swarms_come_from_the_env():
    assert master_fallback.swarms({master_fallback.SWARMS: " a, b ,,"}) == ["a", "b"]
    assert master_fallback.swarms({}) == []


def test_no_master_for_the_window_starts_exactly_one_master():
    store, down = Store({"sw": [Agent("master@x-1")]}), {}
    started, launch = _launches(3)
    env = {master_fallback.SWARMS: "sw", master_fallback.MINUTES: "20"}

    assert master_fallback.check(store, env, set, 0, down, launch) == []
    assert master_fallback.check(store, env, set, 20 * MINUTE - 1, down, launch) == []
    lines = master_fallback.check(store, env, set, 20 * MINUTE, down, launch)

    assert started == ["sw"]
    assert lines == ['{"swarm": "sw", "master_up": 3, "at": 1200000}']
    assert down == {"sw": 20 * MINUTE}
    assert master_fallback.check(store, env, set, 21 * MINUTE, down, launch) == []
    assert started == ["sw"]


def test_a_live_master_resets_the_wait():
    store, down = Store({"sw": [Agent("master@x-1"), Agent("engineer@x-2", "eng")]}), {}
    started, launch = _launches()
    env = {master_fallback.SWARMS: "sw"}

    master_fallback.check(store, env, lambda: {"master@x-1"}, 0, down, launch)
    assert down == {}
    master_fallback.check(store, env, set, 0, down, launch)
    master_fallback.check(store, env, lambda: {"master@x-1"}, 15 * MINUTE, down, launch)
    master_fallback.check(store, env, set, 30 * MINUTE, down, launch)

    assert started == []
    assert down == {"sw": 30 * MINUTE}


def test_a_live_engineer_is_no_master():
    store = Store({"sw": [Agent("engineer@x-2", "eng")]})

    assert master_fallback.master_live(store, "sw", {"engineer@x-2"}) is False
    assert master_fallback.master_live(Store({"sw": [Agent("master@x-1")]}), "sw", {"master@x-1"}) is True


@pytest.mark.parametrize("state", ["stopped", "stopping"])
def test_a_stopped_swarm_is_left_alone(state):
    store, down = Store({"sw": []}, {"sw": state}), {"sw": 0}
    started, launch = _launches()
    env = {master_fallback.SWARMS: "sw"}

    assert master_fallback.check(store, env, set, 60 * MINUTE, down, launch) == []
    assert started == []
    assert down == {}
    assert master_fallback.check(store, env, set, 60 * MINUTE, down, launch) == []


def test_a_stopped_swarm_does_not_end_the_pass_for_the_swarms_after_it():
    store, down = Store({"a": [], "b": []}, {"a": "stopped"}), {"a": 0, "b": 0}
    started, launch = _launches(5)
    env = {master_fallback.SWARMS: "a,b"}

    assert master_fallback.check(store, env, set, 60 * MINUTE, down, launch) == [
        '{"swarm": "b", "master_up": 5, "at": 3600000}'
    ]
    assert started == ["b"]


def test_the_service_runs_the_loop_with_the_watched_swarms_and_minutes():
    text = master_fallback.unit_text("/py", "/repo", {master_fallback.SWARMS: "sw", master_fallback.MINUTES: "20"})

    assert text == (
        "[Unit]\nDescription=agentihooks master fallback tick\n\n"
        "[Service]\n"
        "WorkingDirectory=/repo\n"
        "Environment=AGENTIHOOKS_MASTER_FALLBACK_SWARMS=sw\n"
        "Environment=AGENTIHOOKS_MASTER_FALLBACK_MINUTES=20\n"
        'ExecStart="/py" -m scripts.swarm.master_fallback run\n'
        "Restart=always\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    assert "SWARMS=\n" in master_fallback.unit_text("/py", "/repo", {})


def test_the_outage_starts_survive_between_passes(tmp_path):
    state = tmp_path / "fallback.json"

    assert master_fallback.load(state) == {}
    state.write_text('{"sw": 5}')
    assert master_fallback.load(state) == {"sw": 5}


class Done(Exception):
    pass


def test_start_launches_master_up_new_in_its_own_session(monkeypatch):
    calls = []

    def popen(command, start_new_session):
        calls.append((command, start_new_session))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(master_fallback.subprocess, "Popen", popen)

    assert master_fallback.start("sw") == 4321
    assert calls == [(["agentihooks", "swarm", "sw", "master", "up", "--new"], True)]


def test_install_writes_and_restarts_the_service_and_retires_the_timer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(master_fallback.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(master_fallback.subprocess, "run", lambda command, check: calls.append((command, check)))
    folder = tmp_path / ".config" / "systemd" / "user"
    folder.mkdir(parents=True)
    (folder / "agentihooks-master-fallback.timer").write_text("old")
    env = {master_fallback.SWARMS: "sw"}

    path = master_fallback.install(env)

    repo = str(Path(master_fallback.__file__).resolve().parents[2])
    assert path == folder / "agentihooks-master-fallback.service"
    assert path.read_text() == master_fallback.unit_text(sys.executable, repo, env)
    assert not (folder / "agentihooks-master-fallback.timer").exists()
    assert calls == [
        (["systemctl", "--user", "disable", "--now", "agentihooks-master-fallback.timer"], False),
        (["systemctl", "--user", "daemon-reload"], True),
        (["systemctl", "--user", "enable", "agentihooks-master-fallback.service"], True),
        (["systemctl", "--user", "restart", "agentihooks-master-fallback.service"], True),
    ]


def test_install_makes_the_unit_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(master_fallback.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(master_fallback.subprocess, "run", lambda command, check: None)

    assert master_fallback.install({}).exists()


def test_main_installs_passes_or_runs(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(master_fallback, "install", lambda environ: seen.append(("install", environ)) or "/unit")
    monkeypatch.setattr(master_fallback, "pass_once", lambda environ: seen.append(("pass", environ)))
    monkeypatch.setattr(master_fallback, "run", lambda environ: seen.append(("run", environ)))

    master_fallback.main(["install"])
    master_fallback.main(["pass"])
    master_fallback.main([])
    master_fallback.main(["install", "pass"])

    environ = master_fallback.os.environ
    assert seen == [("install", environ), ("pass", environ), ("run", environ), ("run", environ)]
    assert all(found is environ for _, found in seen)
    assert capsys.readouterr().out == "/unit\n"


def test_run_makes_each_pass_in_its_own_bounded_process_and_waits_a_minute(monkeypatch):
    from scripts.scheduler import process_timer

    runs, waits = [], []

    def run(command, check, timeout):
        runs.append((command, check, timeout))

    def wait(delay, timeout):
        waits.append((delay, timeout))
        raise Done

    monkeypatch.setattr(master_fallback.subprocess, "run", run)
    monkeypatch.setattr(process_timer, "wait", wait)

    with pytest.raises(Done):
        master_fallback.run({})

    assert runs == [([sys.executable, "-m", "scripts.swarm.master_fallback", "pass"], False, 50)]
    assert waits == [(60, 120)]


def test_run_logs_a_timed_out_pass_and_goes_on(monkeypatch, capsys):
    from scripts.scheduler import process_timer

    def run(command, check, timeout):
        raise master_fallback.subprocess.TimeoutExpired(command, timeout)

    def wait(delay, timeout):
        raise Done

    monkeypatch.setattr(master_fallback.subprocess, "run", run)
    monkeypatch.setattr(process_timer, "wait", wait)

    with pytest.raises(Done):
        master_fallback.run({})

    assert capsys.readouterr().out == '{"error": "pass timed out after 50s"}\n'


def test_a_pass_starts_a_master_and_saves_its_state(tmp_path, monkeypatch, capsys):
    from scripts.swarm import runtime, store

    class Herdr:
        def live_names(self):
            return set()

    state = tmp_path / "state" / "fallback.json"
    state.parent.mkdir()
    state.write_text('{"sw": 0}')
    monkeypatch.setattr(runtime, "HerdrRuntime", Herdr)
    monkeypatch.setattr(store, "connect", lambda: Store({"sw": []}))
    monkeypatch.setattr(master_fallback, "start", lambda slug: 77)
    monkeypatch.setattr(master_fallback.time, "time", lambda: 1200.0)

    master_fallback.pass_once({master_fallback.SWARMS: "sw"}, state)

    assert capsys.readouterr().out == '{"swarm": "sw", "master_up": 77, "at": 1200000}\n'
    assert master_fallback.load(state) == {"sw": 1200000}


def test_a_failed_pass_is_logged_and_saves_nothing(tmp_path, monkeypatch, capsys):
    from scripts.swarm import runtime, store

    def connect():
        raise RuntimeError("redis down")

    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: SimpleNamespace(live_names=set))
    monkeypatch.setattr(store, "connect", connect)

    master_fallback.pass_once({}, tmp_path / "fallback.json")

    assert capsys.readouterr().out == '{"error": "RuntimeError: redis down"}\n'
    assert not (tmp_path / "fallback.json").exists()


def test_a_first_pass_makes_every_missing_state_folder(tmp_path, monkeypatch):
    from scripts.swarm import runtime, store

    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: SimpleNamespace(live_names=set))
    monkeypatch.setattr(store, "connect", lambda: Store({"sw": []}))
    monkeypatch.setattr(master_fallback.time, "time", lambda: 1200.0)
    state = tmp_path / "home" / ".agentihooks" / "fallback.json"

    master_fallback.pass_once({master_fallback.SWARMS: "sw"}, state)

    assert master_fallback.load(state) == {"sw": 1200000}


def test_the_loop_and_a_pass_flush_every_line_they_log(tmp_path, monkeypatch):
    from scripts.scheduler import process_timer
    from scripts.swarm import runtime, store

    printed = []

    def run(command, check, timeout):
        raise master_fallback.subprocess.TimeoutExpired(command, timeout)

    def wait(delay, timeout):
        raise Done

    def connect():
        raise RuntimeError("redis down")

    monkeypatch.setattr(master_fallback, "print", lambda *args, **kwargs: printed.append(kwargs), raising=False)
    monkeypatch.setattr(master_fallback.subprocess, "run", run)
    monkeypatch.setattr(process_timer, "wait", wait)
    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: SimpleNamespace(live_names=set))
    monkeypatch.setattr(store, "connect", connect)

    with pytest.raises(Done):
        master_fallback.run({})
    master_fallback.pass_once({}, tmp_path / "fallback.json")

    assert printed == [{"flush": True}, {"flush": True}]


def test_a_pass_flushes_each_master_it_started(tmp_path, monkeypatch):
    from scripts.swarm import runtime, store

    printed = []
    state = tmp_path / "fallback.json"
    state.write_text('{"sw": 0}')
    monkeypatch.setattr(master_fallback, "print", lambda *args, **kwargs: printed.append((args, kwargs)), raising=False)
    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: SimpleNamespace(live_names=set))
    monkeypatch.setattr(store, "connect", lambda: Store({"sw": []}))
    monkeypatch.setattr(master_fallback, "start", lambda slug: 77)
    monkeypatch.setattr(master_fallback.time, "time", lambda: 1200.0)

    master_fallback.pass_once({master_fallback.SWARMS: "sw"}, state)

    assert printed == [(('{"swarm": "sw", "master_up": 77, "at": 1200000}',), {"flush": True})]
