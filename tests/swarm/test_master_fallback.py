import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import ledger_client, master_critical, master_fallback
from tests.swarm.client_class import client_class

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
    records: dict = field(default_factory=dict)

    def agents(self, slug):
        return self.agents_by_slug.get(slug, [])

    def config(self, slug):
        return Config(self.states.get(slug, "running"))

    def key(self, slug: str, name: str) -> str:
        return f"{slug}:{name}"

    @property
    def redis(self) -> SimpleNamespace:
        return SimpleNamespace(get=self.records.get)


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


@pytest.mark.feature("master-fallback-tick")
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


@pytest.mark.feature("master-fallback-tick")
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
@pytest.mark.feature("master-fallback-tick")
def test_a_stopped_swarm_is_left_alone(state):
    store, down = Store({"sw": []}, {"sw": state}), {"sw": 0}
    started, launch = _launches()
    env = {master_fallback.SWARMS: "sw"}

    assert master_fallback.check(store, env, set, 60 * MINUTE, down, launch) == []
    assert started == []
    assert down == {}
    assert master_fallback.check(store, env, set, 60 * MINUTE, down, launch) == []


@pytest.mark.feature("master-fallback-tick")
def test_a_stopped_swarm_does_not_end_the_pass_for_the_swarms_after_it():
    store, down = Store({"a": [], "b": []}, {"a": "stopped"}), {"a": 0, "b": 0}
    started, launch = _launches(5)
    env = {master_fallback.SWARMS: "a,b"}

    assert master_fallback.check(store, env, set, 60 * MINUTE, down, launch) == [
        '{"swarm": "b", "master_up": 5, "at": 3600000}'
    ]
    assert started == ["b"]


@pytest.mark.feature("swarm-master-critical")
def test_the_twenty_minute_start_leaves_a_critical_swarm_to_the_forced_start() -> None:
    held = Store({"sw": []}, records={"sw:master-critical": '{"since": 0, "critical_at": 1800000}'})
    down = {"sw": 0}
    started, launch = _launches(3)
    env = {master_fallback.SWARMS: "sw", master_fallback.MINUTES: "20"}

    assert master_fallback.check(held, env, set, 40 * MINUTE, down, launch) == []
    assert (started, down) == ([], {})


@pytest.mark.feature("swarm-master-critical")
def test_each_watched_swarm_gets_its_own_critical_watch_and_watch_beat(monkeypatch: pytest.MonkeyPatch) -> None:
    held, ledger, beats = Store(), object(), []
    env = {master_fallback.SWARMS: "gone,sw"}

    def watch(found: Any, used: Any, slug: str, live: set[str], now_ms: int, environ: Any, force: Any) -> list[str]:
        assert (found, used, live, now_ms, environ, force) == (held, ledger, {"m"}, 5, env, master_fallback.force)
        if slug == "gone":
            raise KeyError("gone")
        return [f"{slug} watched"]

    def beat(found: Any, slug: str, now_ms: int, error: str) -> None:
        beats.append((found, slug, now_ms, error))

    def unwatch(found: Any, watched: list[str]) -> None:
        unwatched.append((found, watched))

    unwatched: list[tuple[Any, list[str]]] = []
    monkeypatch.setattr(master_critical, "watch", watch)
    monkeypatch.setattr(master_critical, "beat", beat)
    monkeypatch.setattr(master_critical, "unwatch", unwatch)

    assert master_fallback.critical(held, ledger, env, {"m"}, 5) == [
        '{"swarm": "gone", "error": "KeyError: \'gone\'"}',
        "sw watched",
    ]
    assert beats == [(held, "gone", 5, "KeyError: 'gone'"), (held, "sw", 5, "")]
    assert unwatched == [(held, ["gone", "sw"])]


@pytest.mark.feature("master-fallback-tick")
def test_the_service_runs_the_loop_with_the_watched_swarms_and_minutes():
    text = master_fallback.unit_text("/py", "/repo", {master_fallback.SWARMS: "sw", master_fallback.MINUTES: "20"})

    assert text == (
        "[Unit]\nDescription=agentihooks master fallback tick\n\n"
        "[Service]\n"
        "WorkingDirectory=/repo\n"
        "Environment=AGENTIHOOKS_MASTER_FALLBACK_SWARMS=sw\n"
        "Environment=AGENTIHOOKS_MASTER_FALLBACK_MINUTES=20\n"
        'ExecStart="/py" -P -m scripts.swarm.master_fallback run\n'
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


@pytest.mark.feature("master-fallback-tick")
def test_start_launches_master_up_new_in_its_own_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []

    def popen(command: list[str], start_new_session: bool, env: dict[str, str]) -> SimpleNamespace:
        calls.append((command, start_new_session, env))
        return SimpleNamespace(pid=4321)

    monkeypatch.setattr(master_fallback, "subprocess", SimpleNamespace(Popen=popen))
    monkeypatch.setattr(master_fallback.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert master_fallback.start("sw") == 4321
    [(command, session, env)] = calls
    assert command == [sys.executable, "-P", "-m", "scripts.install", "swarm", "sw", "master", "up", "--new"]
    assert session is True
    assert env["PATH"] == os.pathsep.join(
        [str(tmp_path / ".local" / "bin"), str(Path(sys.executable).parent), "/usr/bin:/bin"]
    )
    assert {key: value for key, value in env.items() if key != "PATH"} == {
        key: value for key, value in os.environ.items() if key != "PATH"
    }


@pytest.mark.feature("swarm-master-critical")
def test_a_forced_master_up_runs_its_own_runner_in_its_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def popen(command: list[str], start_new_session: bool) -> SimpleNamespace:
        calls.append((command, start_new_session))
        return SimpleNamespace(pid=77)

    monkeypatch.setattr(master_fallback, "subprocess", SimpleNamespace(Popen=popen))

    assert master_fallback.force("sw", 1_801_000) == 77
    assert calls == [([sys.executable, "-P", "-m", "scripts.swarm.master_fallback", "force", "sw", "1801000"], True)]


def test_a_launch_keeps_an_empty_service_path_after_the_user_folders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(master_fallback.Path, "home", lambda: tmp_path)

    assert master_fallback.launch_env({"A": "1"}) == {
        "A": "1",
        "PATH": os.pathsep.join([str(tmp_path / ".local" / "bin"), str(Path(sys.executable).parent), ""]),
    }


SYSTEMD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"


def service_path() -> str:
    return os.pathsep.join(folder for folder in SYSTEMD_PATH.split(":") if not (Path(folder) / "agentihooks").exists())


@pytest.mark.feature("master-fallback-tick")
def test_the_installed_unit_starts_master_up_under_the_systemd_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = str(Path(master_fallback.__file__).resolve().parents[2])
    lines = master_fallback.unit_text(sys.executable, repo, {master_fallback.SWARMS: "sw"}).splitlines()
    folder = next(line.removeprefix("WorkingDirectory=") for line in lines if line.startswith("WorkingDirectory="))
    for line in lines:
        if line.startswith("Environment="):
            monkeypatch.setenv(*line.removeprefix("Environment=").split("=", 1))
    monkeypatch.setenv("PATH", service_path())
    monkeypatch.setenv("HOME", str(tmp_path))
    started: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        master_fallback,
        "subprocess",
        SimpleNamespace(Popen=lambda command, **options: started.append((command, options)) or SimpleNamespace(pid=9)),
    )

    assert master_fallback.start("sw") == 9

    [(command, options)] = started
    environ: dict[str, str] = options.get("env", dict(os.environ))
    done = subprocess.run(
        [*command[:-1], "--help"], cwd=folder, env=environ, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "--replace" in done.stdout
    assert command[-5:] == ["swarm", "sw", "master", "up", "--new"]


@pytest.mark.feature("master-fallback-tick")
def test_master_up_runs_the_installed_package_past_a_scripts_folder_in_the_working_folder(tmp_path: Path) -> None:
    shadow = tmp_path / "scripts"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")
    (shadow / "install.py").write_text('raise SystemExit("shadowed")\n')

    done = subprocess.run(
        [*master_fallback.master_up("sw", "--new")[:-1], "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "--replace" in done.stdout


@pytest.mark.feature("master-fallback-tick")
def test_install_writes_and_restarts_the_service_and_retires_the_timer(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(master_fallback.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        master_fallback.subprocess, "run", lambda command, check, timeout: calls.append((command, check, timeout))
    )
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
        (["systemctl", "--user", "disable", "--now", "agentihooks-master-fallback.timer"], False, 60),
        (["systemctl", "--user", "daemon-reload"], True, 60),
        (["systemctl", "--user", "enable", "agentihooks-master-fallback.service"], True, 60),
        (["systemctl", "--user", "restart", "agentihooks-master-fallback.service"], True, 60),
    ]


def test_install_makes_the_unit_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(master_fallback.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(master_fallback.subprocess, "run", lambda command, check, timeout: None)

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


@pytest.mark.feature("master-fallback-tick")
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

    assert runs == [([sys.executable, "-P", "-m", "scripts.swarm.master_fallback", "pass"], False, 50)]
    assert waits == [(60, 120)]


@pytest.mark.feature("master-fallback-tick")
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


def test_a_pass_starts_a_master_and_saves_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import runtime, store

    class Herdr:
        def live_names(self) -> set[str]:
            return set()

    state = tmp_path / "state" / "fallback.json"
    state.parent.mkdir()
    state.write_text('{"sw": 0}')
    held, ledger, seen = Store({"sw": []}), object(), []
    monkeypatch.setattr(runtime, "HerdrRuntime", Herdr)
    monkeypatch.setattr(store, "connect", lambda: held)
    monkeypatch.setattr(ledger_client, "LedgerClient", client_class(ledger))
    monkeypatch.setattr(master_fallback, "start", lambda slug: 77)
    monkeypatch.setattr(master_fallback.time, "time", lambda: 1200.0)
    monkeypatch.setattr(
        master_fallback,
        "critical",
        lambda *passed: seen.append(passed) or ["critical line"],
    )
    env = {master_fallback.SWARMS: "sw"}

    master_fallback.pass_once(env, state)

    assert capsys.readouterr().out == 'critical line\n{"swarm": "sw", "master_up": 77, "at": 1200000}\n'
    assert master_fallback.load(state) == {"sw": 1200000}
    assert seen == [(held, ledger, env, set(), 1200000)]


def test_a_failed_pass_is_logged_and_saves_nothing(tmp_path, monkeypatch, capsys):
    from scripts.swarm import runtime, store

    def connect():
        raise RuntimeError("redis down")

    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: SimpleNamespace(live_names=set))
    monkeypatch.setattr(store, "connect", connect)

    master_fallback.pass_once({}, tmp_path / "fallback.json")

    assert capsys.readouterr().out == '{"error": "RuntimeError: redis down"}\n'
    assert not (tmp_path / "fallback.json").exists()


def test_a_first_pass_makes_every_missing_state_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import runtime, store

    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: SimpleNamespace(live_names=set))
    monkeypatch.setattr(store, "connect", lambda: Store({"sw": []}))
    monkeypatch.setattr(ledger_client, "LedgerClient", object)
    monkeypatch.setattr(master_fallback.time, "time", lambda: 1200.0)
    monkeypatch.setattr(master_fallback, "critical", lambda found, ledger, environ, live, now_ms: [])
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


def test_a_pass_flushes_each_master_it_started_and_each_critical_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import runtime, store

    printed = []
    state = tmp_path / "fallback.json"
    state.write_text('{"sw": 0}')
    monkeypatch.setattr(master_fallback, "print", lambda *args, **kwargs: printed.append((args, kwargs)), raising=False)
    monkeypatch.setattr(runtime, "HerdrRuntime", lambda: SimpleNamespace(live_names=set))
    monkeypatch.setattr(store, "connect", lambda: Store({"sw": []}))
    monkeypatch.setattr(ledger_client, "LedgerClient", object)
    monkeypatch.setattr(master_fallback, "start", lambda slug: 77)
    monkeypatch.setattr(master_fallback.time, "time", lambda: 1200.0)
    monkeypatch.setattr(master_fallback, "critical", lambda found, ledger, environ, live, now_ms: ["critical line"])

    master_fallback.pass_once({master_fallback.SWARMS: "sw"}, state)

    assert printed == [
        (("critical line",), {"flush": True}),
        (('{"swarm": "sw", "master_up": 77, "at": 1200000}',), {"flush": True}),
    ]
