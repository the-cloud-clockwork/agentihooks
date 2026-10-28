import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


async def test_one_runner_drains_work_and_refuses_a_second_runner(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    stop = asyncio.Event()
    calls = []

    def drain():
        calls.append("drained")
        stop.set()

    scheduler = SimpleNamespace(run_due=drain)
    monkeypatch.setattr(
        service,
        "asyncio",
        SimpleNamespace(
            to_thread=asyncio.to_thread,
            wait_for=asyncio.wait_for,
            Event=Mock(side_effect=AssertionError("supplied stop was ignored")),
        ),
    )
    with service.machine_lock("runner"):
        with pytest.raises(service.AlreadyRunning, match="^scheduler runner is already running$"):
            await service.run(scheduler, stop)
    await service.run(scheduler, stop)
    assert calls == ["drained"]
    with service.machine_lock("runner"):
        pass


async def test_runner_polls_again_after_the_wait_timeout(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    stop = asyncio.Event()
    timeouts = []
    drain = Mock()

    async def wait_for(awaitable, *, timeout):
        awaitable.close()
        timeouts.append(timeout)
        if len(timeouts) == 1:
            raise TimeoutError
        stop.set()

    monkeypatch.setattr(service, "asyncio", SimpleNamespace(to_thread=asyncio.to_thread, wait_for=wait_for))
    await service.run(SimpleNamespace(run_due=drain), stop)
    assert timeouts == [1, 1]
    assert drain.call_count == 2


@pytest.mark.parametrize("failure", ["ConnectionError", "TimeoutError"])
async def test_runner_survives_a_redis_that_does_not_answer_yet(monkeypatch, tmp_path, capsys, failure):
    from redis import exceptions

    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    stop, passes = asyncio.Event(), []

    def drain():
        passes.append(1)
        if len(passes) == 1:
            raise getattr(exceptions, failure)("Timeout reading from socket")
        stop.set()

    await service.run(SimpleNamespace(run_due=drain), stop)
    assert passes == [1, 1]
    assert capsys.readouterr().err == "scheduler pass skipped: the state store did not answer\n"


async def test_runner_still_stops_on_any_other_error(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)

    def drain():
        raise KeyError("pending")

    with pytest.raises(KeyError):
        await service.run(SimpleNamespace(run_due=drain), asyncio.Event())


def test_supervisor_restarts_an_exited_runner_without_machine_timers(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(service, "revision", lambda root: "loaded")
    children = [Mock(), Mock()]
    children[0].poll.return_value = 1
    children[1].poll.return_value = 2
    spawn = Mock(side_effect=children)
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL))
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    monkeypatch.setattr(service, "Event", Mock(side_effect=AssertionError("supplied stop was ignored")))
    stop.wait.side_effect = [False, True]
    service.supervise(stop)
    assert spawn.call_count == 2
    assert [call.args[0] for call in spawn.call_args_list] == [
        [sys.executable, "-m", "scripts.scheduler.service", "run"],
        [sys.executable, "-m", "scripts.scheduler.service", "run"],
    ]
    assert all(call.kwargs == {"cwd": Path(service.__file__).resolve().parents[2]} for call in spawn.call_args_list)
    assert [call.args for call in stop.wait.call_args_list] == [(1,), (1,)]
    assert all(not child.terminate.called for child in children)


def test_supervisor_terminates_its_active_child_on_shutdown(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(service, "revision", lambda root: "loaded")
    child = Mock()
    child.poll.side_effect = [None]
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=Mock(return_value=child)))
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    monkeypatch.setattr(service, "Event", Mock(side_effect=AssertionError("supplied stop was ignored")))
    stop.wait.return_value = True
    service.supervise(stop)
    child.terminate.assert_called_once_with()
    child.wait.assert_called_once_with()
    stop.wait.assert_called_once_with(1)


@pytest.mark.parametrize("variable", ["REDIS_URL", "AGENTIHOOKS_HIVE_REDIS_URL", "AGENTIHOOKS_SWARM_REDIS_URL"])
@pytest.mark.parametrize("inherited", [False, True])
def test_agentihooks_starts_one_detached_supervisor_when_redis_is_configured(
    monkeypatch, tmp_path, variable, inherited
):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(service, "revision", lambda root: "loaded")
    monkeypatch.setattr(service, "installed", lambda: True)
    spawn = Mock()
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL))
    assert service.ensure_running({}) is False
    spawn.assert_not_called()
    environ = {variable: "redis://proof.invalid/0"}
    monkeypatch.setattr(service, "os", SimpleNamespace(environ=environ, getpid=service.os.getpid))
    assert service.ensure_running(None if inherited else environ) is True
    call = spawn.call_args
    assert call.args[0] == [sys.executable, "-m", "scripts.scheduler.service", "supervise"]
    assert call.kwargs["env"] == environ
    assert call.kwargs["cwd"] == Path(service.__file__).resolve().parents[2]
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert call.kwargs["start_new_session"] is True
    assert call.kwargs["stdout"].name == str(tmp_path / "scheduler.log")
    assert call.kwargs["stderr"] is call.kwargs["stdout"]
    assert call.kwargs["stdout"].closed
    with service.machine_lock("startup"):
        assert service.ensure_running(environ) is False
    with service.machine_lock("supervisor"):
        assert service.ensure_running(environ) is False
        stopped = Event()
        stopped.set()
        with pytest.raises(service.AlreadyRunning, match="^scheduler supervisor is already running$"):
            service.supervise(stopped)
    assert spawn.call_count == 1


def test_service_entrypoint_runs_the_configured_redis_scheduler(monkeypatch):
    from scripts.scheduler import service

    redis = object()
    received = []
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: redis)

    async def run(scheduler):
        received.append(scheduler.state.redis)

    monkeypatch.setattr(service, "run", run)
    monkeypatch.setattr(service, "supervise", Mock(side_effect=AssertionError("unexpected supervisor")))
    assert service.main(["run"]) == 0
    assert received == [redis]


def test_service_entrypoint_reports_a_duplicate_runner(monkeypatch, capsys):
    from scripts.scheduler import service

    monkeypatch.setattr("scripts.swarm.store.redis_client", object)

    async def duplicate(scheduler):
        raise service.AlreadyRunning("scheduler runner is already running")

    monkeypatch.setattr(service, "run", duplicate)
    monkeypatch.setattr(service, "supervise", Mock(side_effect=AssertionError("unexpected supervisor")))
    assert service.main(["run"]) == 2
    assert capsys.readouterr().err == "scheduler runner is already running\n"


@pytest.mark.parametrize("args", [["--help"], ["wrong-mode"]])
def test_service_entrypoint_names_its_command(monkeypatch, capsys, args):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "supervise", Mock(side_effect=AssertionError("invalid mode was accepted")))
    with pytest.raises(SystemExit) as result:
        service.main(args)
    output = capsys.readouterr()
    assert result.value.code == (0 if args == ["--help"] else 2)
    assert (output.out or output.err).splitlines()[0] == ("usage: agentihooks scheduler service [-h] {run,supervise}")


def test_supervisor_entrypoint_stops_on_signals_and_restores_handlers(monkeypatch):
    from scripts.scheduler import service

    handlers = {15: object(), 2: object()}
    previous = dict(handlers)

    def wire(number, handler):
        handlers[number] = handler

    monkeypatch.setattr(service, "signal", SimpleNamespace(SIGTERM=15, SIGINT=2, getsignal=handlers.get, signal=wire))
    calls = []

    def supervise(stop):
        assert stop.is_set() is False
        handlers[15](15, None)
        assert stop.is_set() is True
        stop.clear()
        handlers[2](2, None)
        assert stop.is_set() is True
        calls.append("supervised")

    monkeypatch.setattr(service, "supervise", supervise)
    assert service.main(["supervise"]) == 0
    assert calls == ["supervised"]
    assert handlers == previous


def test_machine_lock_directory_is_shared_across_profile_configurations(monkeypatch):
    from scripts.scheduler import service

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.delenv("AGENTIHOOKS_SWARM_REDIS_URL", raising=False)
    directory = service.runtime_dir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(Path.home() / ".config" / "gh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", "/proof/one")
    monkeypatch.setenv("AGENTIHOOKS_HOME", "/proof/two")
    assert service.runtime_dir() == directory
    assert directory.relative_to(Path.home()).parts == (".cache", "agentihooks", "scheduler")


def test_same_domain_refuses_a_second_runner_and_distinct_domains_coexist(tmp_path, monkeypatch):
    from scripts.scheduler import service

    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    first = {
        "AGENTIHOOKS_DEPLOYMENT": "cloud",
        "AGENTIHOOKS_HIVE_ID": "first",
        "AGENTIHOOKS_HIVE_REDIS_URL": "redis://one.invalid:6380/2",
    }
    other_member = {**first, "AGENTIHOOKS_HIVE_ID": "second"}
    other_endpoint = {**first, "AGENTIHOOKS_HIVE_REDIS_URL": "redis://two.invalid:6380/2"}
    with service.machine_lock("runner", environ=first):
        with pytest.raises(service.AlreadyRunning):
            with service.machine_lock("runner", environ=dict(first)):
                pass
        with service.machine_lock("runner", environ=other_member):
            with service.machine_lock("runner", environ=other_endpoint):
                assert len({service.runtime_dir(env) for env in (first, other_member, other_endpoint)}) == 3


def test_domain_defaults_keep_equivalent_redis_endpoints_together(tmp_path, monkeypatch):
    from scripts.scheduler import service

    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    default = tmp_path / ".cache" / "agentihooks" / "scheduler"
    assert service.runtime_dir({}) == default
    assert service.runtime_dir({"AGENTIHOOKS_HIVE_ID": "member"}) == default
    assert service.runtime_dir({"AGENTIHOOKS_DEPLOYMENT": "cloud"}) == default
    endpoint = service.runtime_dir({"AGENTIHOOKS_SWARM_REDIS_URL": "redis://scheduler.invalid"})
    assert endpoint.name == "ea64452fbe69cb75783b66872df777d82305b62c912ea1d2a82decf5afde3703"
    assert service.runtime_dir({"AGENTIHOOKS_SWARM_REDIS_URL": "redis://scheduler.invalid:6379/0"}) == endpoint
    assert service.runtime_dir({"AGENTIHOOKS_SWARM_REDIS_URL": "rediss://scheduler.invalid:6379/0"}) != endpoint


def test_supervisor_startup_and_log_use_the_explicit_domain(tmp_path, monkeypatch):
    from scripts.scheduler import service

    monkeypatch.setattr(service.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(service, "installed", lambda: True)
    active = {
        "AGENTIHOOKS_DEPLOYMENT": "cloud",
        "AGENTIHOOKS_HIVE_ID": "member",
        "AGENTIHOOKS_HIVE_REDIS_URL": "redis://scheduler.invalid:6380/2",
    }
    spawn = Mock()
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL))
    directory = service.runtime_dir(active)
    with service.machine_lock("startup", environ=active):
        assert service.ensure_running(active) is False
    with service.machine_lock("supervisor", environ=active):
        assert service.ensure_running(active) is False
    spawn.assert_not_called()
    assert service.ensure_running(active) is True
    assert spawn.call_args.kwargs["env"] == active
    assert spawn.call_args.kwargs["stdout"].name == str(directory / "scheduler.log")
    assert spawn.call_count == 1


def test_a_supervisor_records_the_root_and_revision_it_loaded_before_its_runner(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)

    def revision(root):
        assert service.holder({}) is None, "the supervisor read git while holding its lock"
        return f"revision of {root}"

    monkeypatch.setattr(service, "revision", revision)
    monkeypatch.setattr(service.time, "time", lambda: 1_000.0)
    seen = []
    child = Mock()
    child.poll.side_effect = [None]

    def spawn(argv, **kwargs):
        seen.append(json.loads((tmp_path / "supervisor.json").read_text()))
        return child

    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn))
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.return_value = True
    service.supervise(stop)
    root = Path(service.__file__).resolve().parents[2]
    assert seen == [{"pid": os.getpid(), "root": str(root), "revision": f"revision of {root}", "started": 1_000.0}]


def test_revision_names_the_checked_out_commit_and_nothing_without_one(tmp_path):
    from scripts.scheduler import service

    git = ["git", "-c", "user.name=proof", "-c", "user.email=proof@invalid", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    assert service.revision(tmp_path) == ""
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "proof"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    assert service.revision(tmp_path) == head
    assert service.revision(tmp_path / "missing") == ""


def test_revision_asks_git_once_with_a_bounded_wait(monkeypatch):
    from scripts.scheduler import service

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(service.subprocess, "run", run)
    assert service.revision("/installed") == ""
    assert calls == [
        (["git", "-C", "/installed", "rev-parse", "HEAD"], {"capture_output": True, "text": True, "timeout": 10})
    ]
    monkeypatch.setattr(service.subprocess, "run", Mock(side_effect=FileNotFoundError("git")))
    assert service.revision("/installed") == ""


def test_holder_reads_the_record_of_the_supervisor_holding_its_lock(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    assert service.holder({}) is None
    record = {"pid": os.getpid(), "root": "/installed", "revision": "r1", "started": 5.0}
    with service.machine_lock("supervisor"):
        assert service.holder({}) == {"pid": os.getpid()}
        (tmp_path / "supervisor.json").write_text(json.dumps(record))
        assert service.holder({}) == record
        (tmp_path / "supervisor.json").write_text(json.dumps({**record, "pid": os.getpid() + 1}))
        assert service.holder({}) == {"pid": os.getpid()}
        (tmp_path / "supervisor.json").write_text("{")
        assert service.holder({}) == {"pid": os.getpid()}
        (tmp_path / "supervisor.json").write_text(f"[{os.getpid()}]")
        assert service.holder({}) == {"pid": os.getpid()}
    assert service.holder({}) is None


def test_holder_reads_the_lock_and_record_of_the_given_domain(monkeypatch, tmp_path):
    from scripts.scheduler import service

    domain = {"AGENTIHOOKS_SWARM_REDIS_URL": "redis://domain.invalid/2"}
    monkeypatch.setattr(
        service, "runtime_dir", lambda environ=None: tmp_path / ("domain" if environ == domain else "default")
    )
    (tmp_path / "domain").mkdir()
    with service.machine_lock("supervisor", environ=domain):
        record = {"pid": os.getpid(), "root": "/installed", "revision": "r1", "started": 5.0}
        (tmp_path / "domain" / "supervisor.json").write_text(json.dumps(record))
        assert service.holder(domain) == record
    with service.machine_lock("supervisor"):
        assert service.holder(domain) is None


def test_holder_of_a_lock_with_no_pid_names_pid_zero(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    with (tmp_path / "supervisor.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert service.holder({}) == {"pid": 0}


def _clock(monkeypatch, service, slept):
    now = [0.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(service.time, "sleep", sleep)


def test_drain_terminates_the_supervisor_and_waits_for_its_lock_to_free(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(
        service, "runtime_dir", lambda environ=None: tmp_path / ("given" if environ == {} else "default")
    )
    (tmp_path / "given").mkdir()
    slept, sent = [], []
    _clock(monkeypatch, service, slept)
    held = service.machine_lock("supervisor", environ={})
    held.__enter__()
    monkeypatch.setattr(service.os, "kill", lambda pid, number: sent.append((pid, number)))
    exits = iter([False, True])

    def sleep(seconds):
        slept.append(seconds)
        if next(exits):
            held.__exit__(None, None, None)

    monkeypatch.setattr(service.time, "sleep", sleep)
    assert service.drain(4242, {}) is True
    assert sent == [(4242, signal.SIGTERM)]
    assert slept == [1, 1]


def test_drain_gives_up_after_its_wait_while_the_supervisor_holds_on(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    slept = []
    _clock(monkeypatch, service, slept)
    monkeypatch.setattr(service.os, "kill", lambda pid, number: None)
    with service.machine_lock("supervisor"):
        assert service.drain(4242, {}) is False
    assert slept == [1] * 30


def test_drain_of_a_supervisor_already_gone_needs_no_wait(monkeypatch, tmp_path):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)

    def gone(pid, number):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(service.os, "kill", gone)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: pytest.fail("drain waited"))
    assert service.drain(1, {}) is True


@pytest.mark.parametrize("pid", [0, -1])
def test_drain_never_signals_without_a_supervisor_pid(monkeypatch, tmp_path, pid):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(service.os, "kill", lambda pid, number: pytest.fail("drain signalled a process group"))
    assert service.drain(pid, {}) is False


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
def test_a_signalled_runner_finishes_the_dispatch_in_hand_and_exits(monkeypatch, tmp_path, number):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    dispatched = []

    def run_due():
        if dispatched:
            raise AssertionError("the runner kept dispatching after its signal")
        os.kill(os.getpid(), number)
        dispatched.append("finished")

    def unhandled(number, frame):
        raise AssertionError("the signal reached the process handler")

    previous = signal.signal(number, unhandled)
    try:
        asyncio.run(service.run(SimpleNamespace(run_due=run_due)))
    finally:
        signal.signal(number, previous)
    assert dispatched == ["finished"]
    with service.machine_lock("runner"):
        pass


@pytest.mark.parametrize("installed", [False, True])
def test_only_a_supervisor_off_the_installed_code_is_tied_to_its_starter(monkeypatch, tmp_path, installed):
    from scripts.scheduler import service

    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    monkeypatch.setattr("scripts.install.install_root", lambda: service.ROOT if installed else tmp_path / "installed")
    spawn = Mock()
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn, DEVNULL=subprocess.DEVNULL))
    environ = {"REDIS_URL": "redis://proof.invalid/0", service.STARTER: "7"}
    assert service.ensure_running(environ) is True
    tied = {} if installed else {service.STARTER: str(os.getpid())}
    assert spawn.call_args.kwargs["env"] == {"REDIS_URL": "redis://proof.invalid/0", **tied}
    assert environ[service.STARTER] == "7"


@pytest.mark.parametrize("installed", [False, True])
def test_installed_compares_the_loaded_root_with_the_installed_agentihooks(monkeypatch, tmp_path, installed):
    from scripts.scheduler import service

    monkeypatch.setattr("scripts.install.install_root", lambda: service.ROOT if installed else tmp_path)
    assert service.installed() is installed


def _tied_supervisor(monkeypatch, tmp_path, service, parents, polls):
    monkeypatch.setattr(service, "runtime_dir", lambda *_args: tmp_path)
    monkeypatch.setattr(service, "revision", lambda root: "loaded")
    child = Mock()
    child.poll.side_effect = polls
    spawn = Mock(return_value=child)
    monkeypatch.setattr(service, "subprocess", SimpleNamespace(Popen=spawn))
    ppids = iter(parents)
    monkeypatch.setattr(
        service,
        "os",
        SimpleNamespace(environ={service.STARTER: "41"}, getpid=service.os.getpid, getppid=lambda: next(ppids)),
    )
    stop = Mock(spec=Event)
    stop.is_set.return_value = False
    stop.wait.return_value = False
    service.supervise(stop)
    return child, spawn, stop


def test_a_tied_supervisor_stops_its_runner_once_its_starter_is_gone(monkeypatch, tmp_path):
    from scripts.scheduler import service

    child, spawn, stop = _tied_supervisor(monkeypatch, tmp_path, service, [41, 41, 1], [None, None, None])
    spawn.assert_called_once()
    child.terminate.assert_called_once_with()
    child.wait.assert_called_once_with()
    assert stop.wait.call_count == 3


def test_a_tied_supervisor_ends_between_runners_once_its_starter_is_gone(monkeypatch, tmp_path):
    from scripts.scheduler import service

    child, spawn, stop = _tied_supervisor(monkeypatch, tmp_path, service, [41, 1], [1, 1])
    assert spawn.call_count == 2
    child.terminate.assert_not_called()
    assert stop.wait.call_count == 2
