import os
import signal
import subprocess

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_probe, ledger_watchdog
from scripts.swarm.store import PREFIX, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import server_code
from tests.swarm.test_ledger_probe import Clock, ProbedLedger
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

PID = 4242
ARGV = ["/venv/bin/python3", "/repo/scripts/swarm_ledger/ledger_server.py", "--serve"]


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    s.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "", seat="master@sw"))
    return s


def plant(proc, pid=PID, threads=70, rss_kb=550_000, argv=ARGV, state="S"):
    root = proc / str(pid)
    root.mkdir(parents=True, exist_ok=True)
    (root / "status").write_text(f"Name:\tpython3\nState:\t{state}\nThreads:\t{threads}\nVmRSS:\t  {rss_kb} kB\n")
    (root / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (root / "stat").write_text(f"{pid} (python3 (x) y) {state} 1 2 3")


class Host(ledger_watchdog.Host):
    def __init__(self, tmp_path, survives=False):
        self.calls, self.survives, self.now = [], survives, 0.0
        folder, proc = tmp_path / "ledger", tmp_path / "proc"
        folder.mkdir()
        proc.mkdir()
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
        self.calls.append(("run", argv, kw["env"]["LEDGER_DIR"], kw["timeout"], kw["check"]))
        return subprocess.CompletedProcess(argv, 0)


def current(host, tmp_path):
    code = tmp_path / "code"
    code.mkdir(exist_ok=True)
    (code / "a.py").touch()
    (host.folder / ".server.pid").write_text(str(PID))
    server_code.record(host.folder, PID, (code,))
    return code


def master_mail(store):
    return [i.text for i in InboxStore(store.redis).pending_items("master@sw")]


def test_the_limits_are_set_in_code():
    assert (ledger_watchdog.THREADS_MAX, ledger_watchdog.RSS_MAX_KB) == (150, 1_048_576)
    assert (ledger_watchdog.WRITES, ledger_watchdog.WRITE_SLOW_S) == (3, 2.0)


def test_the_server_threads_memory_and_command_come_from_proc(tmp_path):
    plant(tmp_path, threads=169, rss_kb=1_080_000)
    assert ledger_watchdog.seen(PID, tmp_path) == {"threads": 169, "rss_kb": 1_080_000, "argv": ARGV}
    assert ledger_watchdog.seen(7, tmp_path) is None
    (tmp_path / str(PID) / "status").write_text("Name:\tpython3\n")
    assert ledger_watchdog.seen(PID, tmp_path) is None
    (tmp_path / str(PID) / "status").write_text("Threads:\tmany\n")
    assert ledger_watchdog.seen(PID, tmp_path) is None
    (tmp_path / str(PID) / "status").write_text("Threads:\t9\n")
    assert ledger_watchdog.seen(PID, tmp_path) == {"threads": 9, "rss_kb": 0, "argv": ARGV}


def test_only_a_ledger_server_process_has_a_server_script():
    assert ledger_watchdog.script(ARGV) == ARGV[1]
    assert ledger_watchdog.script(["python3", "-m", "http.server"]) is None
    assert ledger_watchdog.script([]) is None


def test_a_zombie_or_missing_process_is_not_alive(tmp_path):
    assert not ledger_watchdog.alive(PID, tmp_path)
    plant(tmp_path)
    assert ledger_watchdog.alive(PID, tmp_path)
    plant(tmp_path, state="Z")
    assert not ledger_watchdog.alive(PID, tmp_path)


def test_runaway_threads_or_memory_outrank_stale_code(tmp_path):
    folder = tmp_path / "ledger"
    folder.mkdir()
    over = {"threads": 151, "rss_kb": 2_000_000, "argv": ARGV}
    assert ledger_watchdog.why(folder, PID, over) == (
        ledger_watchdog.RUNAWAY,
        "it ran 151 threads, over the limit of 150",
    )
    assert ledger_watchdog.why(folder, PID, {**over, "threads": 150}) == (
        ledger_watchdog.RUNAWAY,
        "it held 1953 megabytes, over the limit of 1024",
    )
    assert ledger_watchdog.why(folder, PID, {**over, "threads": 150, "rss_kb": 1_048_576}) == (
        ledger_watchdog.STALE,
        "its code changed on disk",
    )


def test_a_server_running_the_code_on_disk_needs_nothing(tmp_path):
    folder = tmp_path / "ledger"
    folder.mkdir()
    code = tmp_path / "code"
    code.mkdir()
    server_code.record(folder, PID, (code,))
    assert ledger_watchdog.why(folder, PID, {"threads": 150, "rss_kb": 1_048_576, "argv": ARGV}) is None


def test_an_unreadable_code_tree_skips_the_pass(tmp_path, monkeypatch):
    def broken(folder, pid):
        raise FileNotFoundError("moved")

    monkeypatch.setattr(ledger_watchdog.server_code, "stale", broken)
    assert ledger_watchdog.why(tmp_path, PID, {"threads": 1, "rss_kb": 1, "argv": ARGV}) is None


def test_a_restart_stops_the_server_by_its_own_pid_and_starts_it_from_its_own_script(tmp_path):
    host = Host(tmp_path)
    plant(host.proc)
    ledger_watchdog.restart(host, PID, ARGV)
    assert host.calls == [
        ("kill", PID, signal.SIGTERM),
        ("run", [ARGV[0], ARGV[1], "--ensure"], str(host.folder), 30, False),
    ]


def test_a_server_that_ignores_the_stop_is_killed_after_ten_seconds(tmp_path):
    host = Host(tmp_path, survives=True)
    plant(host.proc)
    ledger_watchdog.restart(host, PID, ARGV)
    assert host.calls == [
        ("kill", PID, signal.SIGTERM),
        *[("sleep", 0.1)] * 100,
        ("kill", PID, signal.SIGKILL),
        ("run", [ARGV[0], ARGV[1], "--ensure"], str(host.folder), 30, False),
    ]


def test_a_server_gone_before_the_stop_or_a_failed_start_still_finishes(tmp_path, capsys):
    host = Host(tmp_path)

    def gone(pid, sig):
        raise ProcessLookupError

    def hung(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 30)

    host.kill, host.run = gone, hung
    ledger_watchdog.restart(host, PID, ARGV)
    assert (
        capsys.readouterr().err
        == "ledger server start failed: Command '"
        + str([ARGV[0], ARGV[1], "--ensure"])
        + "' timed out after 30 seconds\n"
    )


def test_writes_are_timed_three_times_and_failures_named(tmp_path):
    clock = Clock()
    ledger = ProbedLedger(clock, write_s=0.5)
    took = ledger_watchdog.writes(ledger, "sw", {"slots": 2, "ci_minutes": 9.0}, clock)
    assert (took, ledger.writes) == ([(0.5, False)] * 3, [(2, 9.0)] * 3)
    ledger.write_error = SwarmError("ledger sw: ledger server not answering: timed out")
    assert ledger_watchdog.writes(ledger, "sw", {}, clock) == [(0.5, True)] * 3
    assert ledger_watchdog.described([(0.1, False), (2.04, False), (2.5, True)]) == (
        "0.1 seconds, 2.0 seconds and failed after 2.5 seconds"
    )
    assert not ledger_watchdog.slow([(2.0, False)] * 3)
    assert ledger_watchdog.slow([(0.1, False), (2.01, False)])
    assert ledger_watchdog.slow([(0.1, True)])


def test_a_current_server_under_its_limits_is_left_alone(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc)
    current(host, tmp_path)
    ledger = ProbedLedger(Clock())
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == []
    assert host.calls == [] and ledger.writes == [] and master_mail(store) == []


def test_a_stale_server_is_restarted_and_three_fast_writes_alert_nobody(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc)
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


def test_a_slow_write_after_a_stale_restart_alerts_the_master(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc)
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


def test_runaway_threads_restart_the_server_and_alert_the_master_and_operator(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, threads=169)
    current(host, tmp_path)
    ledger = ProbedLedger(Clock())
    assert ledger_watchdog.watch(store, "sw", ledger, FakeRuntime(), host) == [
        "restarted the ledger server because it ran 169 threads, over the limit of 150"
    ]
    text = "The ledger server was restarted because it ran 169 threads, over the limit of 150."
    assert master_mail(store) == [text]
    assert ledger.notes == [ledger_probe.for_operator(text)]
    assert host.calls[0] == ("kill", PID, signal.SIGTERM) and ledger.writes == []


def test_a_failed_operator_notice_is_logged_and_the_restart_still_counts(store, tmp_path, capsys):
    host = Host(tmp_path)
    plant(host.proc, rss_kb=2_097_152)
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
    plant(host.proc, threads=400)
    current(host, tmp_path)
    store.redis.set(ledger_watchdog.LOCK_KEY, 1)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    assert host.calls == []
    store.redis.delete(ledger_watchdog.LOCK_KEY)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) != []
    assert store.redis.get(ledger_watchdog.LOCK_KEY) == str(PID)
    assert 0 < store.redis.pttl(ledger_watchdog.LOCK_KEY) <= 120_000
    assert ledger_watchdog.LOCK_KEY == f"{PREFIX}:ledger-server-restart"


def test_no_pid_file_another_program_or_a_ledger_without_writes_is_left_alone(store, tmp_path):
    host = Host(tmp_path)
    plant(host.proc, threads=400)
    assert ledger_watchdog.watch(store, "sw", ProbedLedger(Clock()), FakeRuntime(), host) == []
    (host.folder / ".server.pid").write_text(str(PID))
    assert ledger_watchdog.watch(store, "sw", FakeLedger([]), FakeRuntime(), host) == []
    plant(host.proc, threads=400, argv=["python3", "-m", "http.server"])
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
