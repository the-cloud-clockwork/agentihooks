import os
import signal
import subprocess

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_probe, ledger_watchdog
from scripts.swarm.ledger_client import LedgerGone, LedgerRefused
from scripts.swarm.store import PREFIX, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import server_code
from tests.swarm.test_ledger_probe import Clock, ProbedLedger
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

PID = 4242
PYTHON = "/venv/bin/python3"


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    s.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "", seat="master@sw"))
    return s


def server_script(tmp_path):
    path = tmp_path / "repo" / "scripts" / "swarm_ledger" / "ledger_server.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return str(path)


def plant(proc, argv, pid=PID, threads=70, rss_kb=550_000, state="S"):
    root = proc / str(pid)
    root.mkdir(parents=True, exist_ok=True)
    (root / "status").write_text(f"Name:\tpython3\nState:\t{state}\nThreads:\t{threads}\nVmRSS:\t  {rss_kb} kB\n")
    (root / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (root / "stat").write_text(f"{pid} (python3 (x) y) {state} 1 2 3")


class Host(ledger_watchdog.Host):
    def __init__(self, tmp_path, survives=False, code=0, stderr=""):
        self.calls, self.survives, self.code, self.stderr, self.now = [], survives, code, stderr, 0.0
        folder, proc = tmp_path / "ledger", tmp_path / "proc"
        folder.mkdir()
        proc.mkdir()
        self.argv = [PYTHON, server_script(tmp_path), "--serve"]
        super().__init__(folder, proc, self.kill, self.run, self.sleep, lambda: self.now)

    def kill(self, pid, sig):
        self.calls.append(("kill", pid, sig))
        if sig == signal.SIGKILL or not self.survives:
            for name in ("status", "cmdline", "stat"):
                (self.proc / str(pid) / name).unlink()
            (self.proc / str(pid)).rmdir()

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        self.now += seconds

    def run(self, argv, **kw):
        self.calls.append(("run", argv, kw["env"]["LEDGER_DIR"], kw["timeout"], kw["check"], kw["text"]))
        (self.folder / ".server.pid").write_text("5151")
        return subprocess.CompletedProcess(argv, self.code, stdout="", stderr=self.stderr)


def current(host, tmp_path):
    code = tmp_path / "code"
    code.mkdir(exist_ok=True)
    (code / "a.py").touch()
    (host.folder / ".server.pid").write_text(str(PID))
    server_code.record(host.folder, PID, server_code.code_stamp((code,)), (code,))
    return code


def master_mail(store):
    return [i.text for i in InboxStore(store.redis).pending_items("master@sw")]


def test_the_limits_are_set_in_code():
    assert (ledger_watchdog.THREADS_MAX, ledger_watchdog.RSS_MAX_KB) == (150, 1_048_576)
    assert (ledger_watchdog.WRITES, ledger_watchdog.WRITE_SLOW_S) == (3, 2.0)
    assert (ledger_watchdog.LOCK_MS, ledger_watchdog.RUNAWAY_MS) == (120_000, 900_000)


def test_the_server_threads_memory_and_command_come_from_proc(tmp_path):
    argv = [PYTHON, "/repo/ledger_server.py", "--serve"]
    plant(tmp_path, argv, threads=169, rss_kb=1_080_000)
    assert ledger_watchdog.seen(PID, tmp_path) == {"threads": 169, "rss_kb": 1_080_000, "argv": argv}
    assert ledger_watchdog.seen(7, tmp_path) is None
    for status in ("Name:\tpython3\n", "Threads:\tmany\n", "Threads:\t9\nVmRSS:\t\n"):
        (tmp_path / str(PID) / "status").write_text(status)
        assert ledger_watchdog.seen(PID, tmp_path) is None
    (tmp_path / str(PID) / "status").write_text("Threads:\t9\n")
    assert ledger_watchdog.seen(PID, tmp_path) == {"threads": 9, "rss_kb": 0, "argv": argv}


def test_only_a_ledger_server_process_has_a_server_script():
    assert ledger_watchdog.script([PYTHON, "/repo/ledger_server.py"]) == "/repo/ledger_server.py"
    assert ledger_watchdog.script(["python3", "-m", "http.server"]) is None
    assert ledger_watchdog.script([]) is None


def test_the_launch_command_needs_the_server_script_on_disk(tmp_path):
    script = server_script(tmp_path)
    assert ledger_watchdog.launch(PID, [PYTHON, script, "--serve"], tmp_path) == [PYTHON, script]
    assert ledger_watchdog.launch(PID, [PYTHON, str(tmp_path / "gone" / "ledger_server.py")], tmp_path) is None
    assert ledger_watchdog.launch(PID, ["python3", "-m", "http.server"], tmp_path) is None
    relative = [PYTHON, "scripts/swarm_ledger/ledger_server.py"]
    assert ledger_watchdog.launch(PID, relative, tmp_path) is None
    (tmp_path / str(PID)).mkdir()
    (tmp_path / str(PID) / "cwd").symlink_to(tmp_path / "repo")
    assert ledger_watchdog.launch(PID, relative, tmp_path) == [PYTHON, script]


def test_a_zombie_or_missing_process_is_not_alive(tmp_path):
    argv = [PYTHON, "/repo/ledger_server.py"]
    assert not ledger_watchdog.alive(PID, tmp_path)
    plant(tmp_path, argv)
    assert ledger_watchdog.alive(PID, tmp_path)
    plant(tmp_path, argv, state="Z")
    assert not ledger_watchdog.alive(PID, tmp_path)


def test_runaway_threads_or_memory_outrank_stale_code(tmp_path):
    over = {"threads": 151, "rss_kb": 2_000_000}
    assert ledger_watchdog.why(tmp_path, PID, over) == (
        ledger_watchdog.RUNAWAY,
        "it ran 151 threads, over the limit of 150",
    )
    assert ledger_watchdog.why(tmp_path, PID, {**over, "threads": 150}) == (
        ledger_watchdog.RUNAWAY,
        "it held 1953 megabytes, over the limit of 1024",
    )
    assert ledger_watchdog.why(tmp_path, PID, {"threads": 150, "rss_kb": 1_048_576}) == (
        ledger_watchdog.STALE,
        "its code changed on disk",
    )


def test_a_server_running_the_code_on_disk_needs_nothing(tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    server_code.record(tmp_path, PID, 0, (code,))
    assert ledger_watchdog.why(tmp_path, PID, {"threads": 150, "rss_kb": 1_048_576}) is None


def test_an_unreadable_code_tree_skips_the_pass(tmp_path, monkeypatch):
    def broken(folder, pid):
        raise FileNotFoundError("moved")

    monkeypatch.setattr(ledger_watchdog.server_code, "stale", broken)
    assert ledger_watchdog.why(tmp_path, PID, {"threads": 1, "rss_kb": 1}) is None


def test_a_restart_stops_the_server_by_its_own_pid_and_starts_it_from_its_own_script(tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv)
    assert ledger_watchdog.restart(host, PID, host.argv[:2]) == ""
    assert host.calls == [
        ("kill", PID, signal.SIGTERM),
        ("run", [*host.argv[:2], "--ensure"], str(host.folder), 30, False, True),
    ]


def test_a_server_that_ignores_the_stop_is_killed_after_ten_seconds(tmp_path):
    host = Host(tmp_path, survives=True)
    plant(host.proc, host.argv)
    ledger_watchdog.restart(host, PID, host.argv[:2])
    assert host.calls == [
        ("kill", PID, signal.SIGTERM),
        *[("sleep", 0.1)] * 100,
        ("kill", PID, signal.SIGKILL),
        ("run", [*host.argv[:2], "--ensure"], str(host.folder), 30, False, True),
    ]


def test_a_pid_taken_by_another_program_during_the_stop_is_never_killed(tmp_path):
    host = Host(tmp_path, survives=True)
    plant(host.proc, host.argv)
    host.sleep = lambda seconds: plant(host.proc, ["python3", "-m", "http.server"])
    ledger_watchdog.restart(host, PID, host.argv[:2])
    assert [call for call in host.calls if call[0] == "kill"] == [("kill", PID, signal.SIGTERM)]


def test_a_refused_signal_or_a_server_gone_before_the_stop_still_starts_it(tmp_path):
    host = Host(tmp_path)

    def refused(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    def gone(pid, sig):
        raise ProcessLookupError

    host.kill = refused
    assert ledger_watchdog.restart(host, PID, host.argv[:2]) == ""
    host.kill = gone
    assert ledger_watchdog.restart(host, PID, host.argv[:2]) == ""
    assert [call[0] for call in host.calls] == ["run", "run"]


def test_a_start_that_fails_or_hangs_returns_its_error(tmp_path):
    host = Host(tmp_path, code=1, stderr="ledger server did not answer on http://127.0.0.1:8765\n")
    assert ledger_watchdog.restart(host, PID, host.argv[:2]) == "ledger server did not answer on http://127.0.0.1:8765"
    host.code, host.stderr = 3, ""
    assert ledger_watchdog.restart(host, PID, host.argv[:2]) == "exit code 3"
    host.code, host.stderr = 1, "a" + "x" * 250
    assert ledger_watchdog.restart(host, PID, host.argv[:2]) == "x" * 200

    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    host.run = hung
    assert ledger_watchdog.restart(host, PID, host.argv[:2]) == (
        f"Command '{[*host.argv[:2], '--ensure']}' timed out after 30 seconds"
    )


def test_writes_are_timed_three_times_and_failures_named():
    clock = Clock()
    ledger = ProbedLedger(clock, write_s=0.5)
    took = ledger_watchdog.writes(ledger, "sw", {"slots": 2, "ci_minutes": 9.0}, clock)
    assert (took, ledger.writes) == ([(0.5, False)] * 3, [(2, 9.0)] * 3)
    ledger.write_error = SwarmError("ledger sw: ledger server not answering: timed out")
    assert ledger_watchdog.writes(ledger, "sw", {}, clock) == [(0.5, True)] * 3
    ledger.write_error = LedgerRefused("ledger sw refused: not a seat")
    assert ledger_watchdog.writes(ledger, "sw", {}, clock) == [(0.5, False)] * 3
    ledger.write_error = LedgerGone("ledger sw does not exist")
    assert ledger_watchdog.writes(ledger, "sw", {}, clock) == [(0.5, False)] * 3
    assert ledger_watchdog.described([(0.1, False), (2.04, False), (2.5, True)]) == (
        "0.1 seconds, 2.0 seconds and failed after 2.5 seconds"
    )
    assert not ledger_watchdog.slow([(2.0, False)] * 3)
    assert ledger_watchdog.slow([(0.1, False), (2.01, False)])
    assert ledger_watchdog.slow([(0.1, True)])


def test_a_current_server_under_its_limits_is_left_alone(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv)
    current(host, tmp_path)
    ledger = ProbedLedger(Clock())
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == []
    assert host.calls == [] and ledger.writes == [] and master_mail(store) == []


def test_a_stale_server_is_restarted_and_three_fast_writes_alert_nobody(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv)
    code = current(host, tmp_path)
    os.utime(code / "a.py", ns=(1, 1))
    clock = Clock()
    host.clock = clock
    ledger = ProbedLedger(clock, write_s=0.3)
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == [
        "restarted the ledger server on new code; three writes took 0.3 seconds, 0.3 seconds and 0.3 seconds"
    ]
    assert host.calls[0] == ("kill", PID, signal.SIGTERM)
    assert len(ledger.writes) == 3 and master_mail(store) == [] and ledger.notes == []
    assert store.redis.get(ledger_watchdog.STARTED_KEY) == "5151"


def test_a_slow_write_after_a_stale_restart_alerts_the_master(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv)
    (host.folder / ".server.pid").write_text(str(PID))
    clock = Clock()
    host.clock = clock
    ledger = ProbedLedger(clock, write_s=2.5)
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == [
        "restarted the ledger server on new code; three writes took 2.5 seconds, 2.5 seconds and 2.5 seconds"
    ]
    assert master_mail(store) == [
        "The ledger server restarted on new code and its writes are slow: 2.5 seconds, 2.5 seconds and 2.5 seconds."
    ]
    assert ledger.notes == []


def test_a_server_that_does_not_start_again_alerts_the_master(store, tmp_path):
    host = Host(tmp_path, code=1, stderr="port 8765 busy\n")
    plant(host.proc, host.argv)
    (host.folder / ".server.pid").write_text(str(PID))
    ledger = ProbedLedger(Clock())
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == [
        "stopped the ledger server because its code changed on disk and it did not start again: port 8765 busy"
    ]
    assert master_mail(store) == [
        "The ledger server was stopped because its code changed on disk and did not start again: port 8765 busy."
    ]
    assert ledger.writes == [] and store.redis.get(ledger_watchdog.STARTED_KEY) is None


def test_a_server_the_tick_started_without_its_own_record_is_not_restarted_again(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv)
    (host.folder / ".server.pid").write_text(str(PID))
    store.redis.set(ledger_watchdog.STARTED_KEY, str(PID))
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    code = current(host, tmp_path)
    os.utime(code / "a.py", ns=(1, 1))
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) != []


def test_runaway_threads_restart_the_server_and_alert_the_master_and_operator(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv, threads=169)
    current(host, tmp_path)
    ledger = ProbedLedger(Clock())
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == [
        "restarted the ledger server because it ran 169 threads, over the limit of 150"
    ]
    text = "The ledger server was restarted because it ran 169 threads, over the limit of 150."
    assert master_mail(store) == [text]
    assert ledger.notes == [ledger_probe.for_operator(text)]
    assert host.calls[0] == ("kill", PID, signal.SIGTERM) and ledger.writes == []
    assert 0 < store.redis.pttl(ledger_watchdog.RUNAWAY_KEY) <= 900_000


def test_a_runaway_restart_waits_out_its_cooldown(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv, threads=400)
    current(host, tmp_path)
    store.redis.set(ledger_watchdog.RUNAWAY_KEY, 1)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    assert host.calls == [] and store.redis.get(ledger_watchdog.LOCK_KEY) is None


def test_a_runaway_claim_that_loses_the_lock_keeps_its_cooldown_free(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv, threads=400)
    current(host, tmp_path)
    store.redis.set(ledger_watchdog.LOCK_KEY, 1)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    assert store.redis.get(ledger_watchdog.RUNAWAY_KEY) is None and host.calls == []


def test_a_failed_operator_notice_is_logged_and_the_restart_still_counts(store, tmp_path, capsys):
    host = Host(tmp_path)
    plant(host.proc, host.argv, rss_kb=2_097_152)
    current(host, tmp_path)
    ledger = ProbedLedger(Clock())
    ledger.notify_error = SwarmError("ledger sw: ledger server not answering: timed out")
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == [
        "restarted the ledger server because it held 2048 megabytes, over the limit of 1024"
    ]
    assert capsys.readouterr().err == (
        "ledger restart notice dropped: ledger sw: ledger server not answering: timed out\n"
    )


def test_one_tick_restarts_the_shared_server_while_the_lock_holds(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv)
    (host.folder / ".server.pid").write_text(str(PID))
    store.redis.set(ledger_watchdog.LOCK_KEY, 1)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    assert host.calls == []
    store.redis.delete(ledger_watchdog.LOCK_KEY)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) != []
    assert store.redis.get(ledger_watchdog.LOCK_KEY) == str(PID)
    assert 0 < store.redis.pttl(ledger_watchdog.LOCK_KEY) <= 120_000
    assert ledger_watchdog.LOCK_KEY == f"{PREFIX}:ledger-server-restart"
    assert ledger_watchdog.RUNAWAY_KEY == f"{PREFIX}:ledger-server-runaway"
    assert ledger_watchdog.STARTED_KEY == f"{PREFIX}:ledger-server-started"


def test_no_pid_file_another_program_a_missing_script_or_a_ledger_without_writes_is_left_alone(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, host.argv, threads=400)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    (host.folder / ".server.pid").write_text(str(PID))
    assert ledger_watchdog.watch(store, "sw", FakeLedger([]), FakeRuntime(), host) == []
    plant(host.proc, ["python3", "-m", "http.server"], threads=400)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    plant(host.proc, [PYTHON, str(tmp_path / "removed" / "ledger_server.py")], threads=400)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    (host.folder / ".server.pid").write_text("9999")
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    assert host.calls == []


def test_the_default_host_reads_the_ledger_folder_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    host = ledger_watchdog.default_host()
    assert (host.folder, host.proc) == (tmp_path, ledger_watchdog.PROC)
    assert (host.kill, host.run, host.sleep) == (os.kill, subprocess.run, ledger_watchdog.time.sleep)
    assert host.clock is ledger_watchdog.time.monotonic


def test_the_tick_runs_the_watchdog_before_the_ledger_probe(monkeypatch, store):
    from scripts.swarm import cli
    from tests.inbox.test_wake import FakeHerdr

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    store.update("sw", state="paused")
    order = []
    monkeypatch.setattr(cli.ledger_watchdog, "watch", lambda *a: order.append("watch") or ["restarted"])
    monkeypatch.setattr(cli.ledger_probe, "observe", lambda *a: order.append("probe") or [])
    actions = cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert order[:2] == ["watch", "probe"] and actions[0] == "restarted"
