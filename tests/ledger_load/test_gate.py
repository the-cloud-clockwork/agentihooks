import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from tests.ledger_load import gate as ledger_load


def test_p95_is_the_nearest_rank_sample():
    samples = [float(n) for n in range(1, 21)]
    assert ledger_load.p95(samples) == 19.0
    assert ledger_load.p95([0.5]) == 0.5


def test_writes_and_cpu_within_budget_pass():
    assert ledger_load.verdict([0.1] * 100, 100, ledger_load.CPU_CORES, []) == []


@pytest.mark.parametrize(
    "writes, cores, errors, problem",
    [
        ([0.1] * 94 + [ledger_load.WRITE_P95_S + 0.01] * 6, 0.2, [], "write p95"),
        ([0.1] * 100, ledger_load.CPU_CORES + 0.01, [], "server CPU"),
        ([0.1] * 100, 0.2, ["ci@ab0000-0001: timed out"], "requests failed"),
        ([], 0.2, [], "writes completed"),
        ([0.1] * 89, 0.2, [], "writes completed"),
    ],
)
def test_a_broken_budget_a_failed_request_or_a_shortfall_is_red(writes, cores, errors, problem):
    problems = ledger_load.verdict(writes, 100, cores, errors)
    assert any(problem in line for line in problems)


def test_cpu_is_judged_on_its_busiest_window_not_the_whole_run():
    calm = [(float(t), 0.2 * t) for t in range(60)]
    burst = [(60.0 + t, calm[-1][1] + 1.5 * (t + 1)) for t in range(ledger_load.CPU_WINDOW_S + 1)]
    assert ledger_load.busiest_window(calm) == pytest.approx(0.2)
    assert ledger_load.busiest_window(calm + burst) > ledger_load.CPU_CORES


def test_a_run_too_short_for_one_cpu_window_is_red():
    assert ledger_load.busiest_window([(0.0, 0.0), (1.0, 0.1)]) is None
    assert any("no 10s window" in line for line in ledger_load.verdict([0.1] * 100, 100, None, []))


def test_alerts_past_their_quiet_hour_left_open_after_the_load_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_load, "TASKS", 20)
    monkeypatch.setattr(ledger_load, "LEDGER_BYTES", 0)
    at = ledger_load.ALERT_QUIET_MS * 10
    ledger_load.store(tmp_path, ledger_load.full_size(at))
    stale = ledger_load.unexpired(tmp_path, at)
    assert stale and all(alert_id.startswith("al-") for alert_id in stale)


def test_the_load_covers_every_sweep_of_the_watch_loop_more_than_once():
    interval = inspect.signature(ledger_server.watch_ledgers).parameters["interval"].default
    period = ledger_server.BIN_SWEEP_EVERY * interval
    assert ledger_load.duration() >= ledger_load.SWEEPS * period + period / 2


def test_the_gate_refuses_to_run_outside_ci(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(ledger_load, "run", lambda folder: ran.append(folder) or ([], 0.1))
    monkeypatch.setattr(ledger_load, "base_budgets", lambda revision: head_budgets())
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit, match="CI only"):
        ledger_load.main(["--folder", str(tmp_path), "--base", "HEAD"])
    assert ran == []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ledger_load.main(["--folder", str(tmp_path), "--base", "HEAD"]) == 0
    assert ran == [tmp_path]


def head_budgets() -> dict:
    return {name: getattr(ledger_load, name) for name in ledger_load.BUDGETS}


def test_a_head_that_loosens_its_budgets_is_graded_by_the_base(monkeypatch):
    base = head_budgets()
    monkeypatch.setattr(ledger_load, "WRITE_P95_S", base["WRITE_P95_S"] * 10)
    monkeypatch.setattr(ledger_load, "CPU_CORES", base["CPU_CORES"] * 10)
    monkeypatch.setattr(ledger_load, "LEDGER_BYTES", base["LEDGER_BYTES"] // 10)
    assert ledger_load.graded(base) == base


def test_a_head_that_tightens_its_budgets_keeps_its_own(monkeypatch):
    base = head_budgets()
    tight = {"WRITE_P95_S": base["WRITE_P95_S"] / 2, "CPU_CORES": base["CPU_CORES"] / 2}
    tight["LEDGER_BYTES"] = base["LEDGER_BYTES"] * 2
    tight["MIN_WRITES"] = 1.0
    for name, value in tight.items():
        monkeypatch.setattr(ledger_load, name, value)
    assert ledger_load.graded(base) == {**base, **tight}


def test_every_load_shape_constant_a_head_could_loosen_is_graded_from_the_base(monkeypatch):
    base = head_budgets()
    loose = {"CPU_WINDOW_S": 600, "CLIENTS": 1, "LIVE_PEAK_WRITES_PER_MINUTE": 1, "HEADROOM": 0.1, "MIN_WRITES": 0}
    loose |= {"READS_PER_SECOND": 0.01, "SWEEPS": 0}
    for name, value in loose.items():
        monkeypatch.setattr(ledger_load, name, value)
    assert ledger_load.graded(base) == base


def test_the_base_budgets_are_read_from_the_commit_not_the_working_file(monkeypatch, tmp_path):
    gate = tmp_path / "tests" / "ledger_load" / "gate.py"
    gate.parent.mkdir(parents=True)
    committed = Path(ledger_load.__file__).read_text().replace("CPU_CORES = 1.0", "CPU_CORES = 0.25")
    gate.write_text(committed)
    git = ["git", "-C", str(tmp_path), "-c", "user.email=gate@example.com", "-c", "user.name=gate"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    gate.write_text(committed.replace("CPU_CORES = 0.25", "CPU_CORES = 9.0"))
    monkeypatch.setattr(ledger_load, "__file__", str(gate))
    assert ledger_load.base_budgets("HEAD")["CPU_CORES"] == 0.25


def test_budgets_are_read_from_the_source_given_not_the_running_module():
    planted = {name: n + 0.5 for n, name in enumerate(ledger_load.BUDGETS)}
    source = "".join(f"{name} = {value}\n" for name, value in planted.items()) + "TIMEOUT_S = 99.0\n"
    assert ledger_load.budgets(source) == planted


def test_a_base_without_the_gate_is_red():
    with pytest.raises(SystemExit, match="could not show the ledger load gate"):
        ledger_load.base_budgets("0" * 40)


def test_a_base_without_a_budget_is_red():
    with pytest.raises(SystemExit, match="LEDGER_BYTES"):
        ledger_load.budgets("WRITE_P95_S = 2.0\nCPU_CORES = 1.0\n")


def test_the_base_budgets_come_from_the_committed_gate_at_that_revision():
    assert ledger_load.base_budgets("HEAD") == ledger_load.budgets(Path(ledger_load.__file__).read_text())


def test_the_gate_runs_on_the_stricter_of_base_and_head_budgets(monkeypatch, tmp_path):
    for name, value in head_budgets().items():
        monkeypatch.setattr(ledger_load, name, value)
    strict = {**head_budgets(), "WRITE_P95_S": 0.5, "CPU_CORES": 0.25, "LEDGER_BYTES": 10**8}
    revisions = []
    monkeypatch.setattr(ledger_load, "base_budgets", lambda revision: revisions.append(revision) or strict)
    monkeypatch.setattr(
        ledger_load, "run", lambda folder: ([] if head_budgets() == strict else ["head budgets graded"], 0.1)
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert ledger_load.main(["--folder", str(tmp_path), "--base", "abc123"]) == 0
    assert revisions == ["abc123"]


BREACH = ledger_load.WRITE_P95_S + 0.5


def breach(p95: float) -> str:
    return f"{ledger_load.WRITE_BREACH} {p95:.3f}s exceeds {ledger_load.WRITE_P95_S}s"


def graded_main(monkeypatch, tmp_path, *outcomes) -> tuple[int, list[Path]]:
    calls = []

    def measure(folder):
        calls.append(folder)
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(ledger_load, "run", measure)
    monkeypatch.setattr(ledger_load, "base_budgets", lambda revision: head_budgets())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    return ledger_load.main(["--folder", str(tmp_path), "--base", "HEAD"]), calls


def test_a_lone_write_breach_repeats_once_on_a_fresh_ledger_and_the_better_measure_passes(
    monkeypatch, tmp_path, capsys
):
    code, calls = graded_main(monkeypatch, tmp_path, ([breach(BREACH)], BREACH), ([], 0.4))
    out = capsys.readouterr().out
    assert code == 0
    assert calls == [tmp_path, tmp_path / "repeat"]
    assert "::notice::write p95 alone broke its budget, so the gate repeated the measure once on a fresh ledger" in out
    assert "graded the repeat, which passes only when the better of the two is within budget" in out
    assert f"::notice::write p95 first {BREACH:.3f}s, repeat 0.400s, budget {ledger_load.WRITE_P95_S}s" in out


def test_a_write_breach_on_both_measures_is_red_and_reports_both_values(monkeypatch, tmp_path, capsys):
    code, calls = graded_main(monkeypatch, tmp_path, ([breach(BREACH)], BREACH), ([breach(3.0)], 3.0))
    out = capsys.readouterr().out
    assert code == 1
    assert calls == [tmp_path, tmp_path / "repeat"]
    assert f"write p95 first {BREACH:.3f}s, repeat 3.000s" in out
    assert f"::error::{breach(3.0)}" in out


def test_a_write_breach_beside_another_problem_is_red_without_a_repeat(monkeypatch, tmp_path, capsys):
    code, calls = graded_main(monkeypatch, tmp_path, ([breach(BREACH), "1 requests failed, first: x"], BREACH))
    out = capsys.readouterr().out
    assert code == 1
    assert calls == [tmp_path]
    assert "repeated the measure" not in out
    assert f"::error::{breach(BREACH)}" in out


def test_another_problem_alone_is_red_without_a_repeat(monkeypatch, tmp_path):
    code, calls = graded_main(monkeypatch, tmp_path, (["1 requests failed, first: x"], 0.3))
    assert code == 1
    assert calls == [tmp_path]


def test_a_repeat_with_no_writes_reports_it_and_stays_red(monkeypatch, tmp_path, capsys):
    shortfall = "0 writes completed of 100 expected"
    code, _ = graded_main(monkeypatch, tmp_path, ([breach(BREACH)], BREACH), ([shortfall], None))
    out = capsys.readouterr().out
    assert code == 1
    assert f"write p95 first {BREACH:.3f}s, repeat no writes" in out


def test_the_write_breach_problem_names_its_measured_p95():
    problems = ledger_load.verdict([0.1] * 94 + [BREACH] * 6, 100, 0.2, [])
    assert problems == ["write p95 2.500s exceeds 2.0s"]


def test_the_reported_write_p95_is_none_without_writes():
    assert ledger_load.p95_or_none([]) is None
    assert ledger_load.p95_or_none([0.3, 0.1, 0.2]) == 0.3


def test_a_run_returns_its_problems_and_its_measured_write_p95(monkeypatch, tmp_path):
    results = {"writes": [0.1] * 9 + [0.7], "reads": [0.05] * 10, "item_reads": [0.01] * 100, "errors": []}
    results["cpu"] = [(0.0, 0.0), (float(ledger_load.CPU_WINDOW_S), 1.0)]
    results["resources"] = resource_samples()
    monkeypatch.setattr(ledger_load, "full_size", lambda at: {"tasks": []})
    monkeypatch.setattr(ledger_load, "store", lambda folder, doc: "token")
    monkeypatch.setattr(ledger_load, "free_port", lambda: 0)
    monkeypatch.setattr(ledger_load, "duration", lambda: 10.0)
    monkeypatch.setattr(ledger_load, "start_server", lambda folder, port, log: SimpleNamespace(pid=1))
    monkeypatch.setattr(ledger_load, "stop_server", lambda server: None)
    monkeypatch.setattr(ledger_load, "load", lambda port, token, pid, seconds: results)
    monkeypatch.setattr(ledger_load, "unexpired", lambda folder, at: [])
    assert ledger_load.run(tmp_path) == ([], 0.7)


def resource_samples(threads=0, memory=0):
    return [(float(n), 12 + threads * n, 100_000 + memory * n) for n in range(91)]


def test_stable_server_resources_and_initial_warmup_pass():
    samples = [(at, count, rss + (50_000 if at >= 10 else 0)) for at, count, rss in resource_samples()]
    assert ledger_load.resource_verdict(samples) == []


def test_continuing_thread_growth_is_red():
    problems = ledger_load.resource_verdict(resource_samples(threads=0.5))
    assert len(problems) == 1
    assert "server threads kept growing" in problems[0]


def test_continuing_memory_growth_is_red():
    problems = ledger_load.resource_verdict(resource_samples(memory=2048))
    assert len(problems) == 1
    assert "server RSS kept growing" in problems[0]


def test_a_temporary_resource_peak_that_recovers_passes():
    samples = [(at, count + (100 if 40 <= at <= 45 else 0), rss) for at, count, rss in resource_samples()]
    assert ledger_load.resource_verdict(samples) == []


def test_missing_or_short_resource_measurements_are_red():
    assert ledger_load.resource_verdict([]) == ["server resource sampling did not cover 60s after warmup"]
    assert ledger_load.resource_verdict(resource_samples()[:40]) == [
        "server resource sampling did not cover 60s after warmup"
    ]


def test_resource_limits_cannot_be_loosened_by_the_head(monkeypatch):
    base = head_budgets()
    for name, value in {
        "RESOURCE_WARMUP_S": 40,
        "RESOURCE_WINDOW_S": 10,
        "THREAD_GROWTH": 1000,
        "RSS_GROWTH_KIB": 1_000_000,
    }.items():
        monkeypatch.setattr(ledger_load, name, value)
    assert ledger_load.graded(base) == base


def test_the_gate_before_resource_sampling_uses_fixed_initial_resource_limits():
    source = Path(ledger_load.__file__).read_text()
    source = "\n".join(
        line
        for line in source.splitlines()
        if not line.startswith(("RESOURCE_WARMUP_S =", "RESOURCE_WINDOW_S =", "THREAD_GROWTH =", "RSS_GROWTH_KIB ="))
    )
    assert ledger_load.budgets(source) == head_budgets()


def test_a_partially_missing_resource_budget_is_red():
    source = Path(ledger_load.__file__).read_text().replace("THREAD_GROWTH = 8", "")
    with pytest.raises(SystemExit, match="THREAD_GROWTH"):
        ledger_load.budgets(source)


def test_some_open_alerts_are_past_their_quiet_hour_so_the_expiry_sweep_has_work():
    at = 10 * ledger_load.ALERT_QUIET_MS
    alerts = [ledger_load.alert(n, at) for n in range(ledger_load.ALERTS)]
    assert any(ledger_server.ledger_alerts.expired(alert, at) for alert in alerts)
    assert not all(ledger_server.ledger_alerts.expired(alert, at) for alert in alerts if alert["state"] == "open")


def test_the_generated_ledger_reaches_the_target_size(monkeypatch):
    monkeypatch.setattr(ledger_load, "TASKS", 40)
    target = ledger_load.size(ledger_load.document(1)) + 200_000
    monkeypatch.setattr(ledger_load, "LEDGER_BYTES", target)
    doc = ledger_load.full_size(1)
    assert len(doc["tasks"]) == 40
    assert target <= ledger_load.size(doc) < target + 40 * 100


def test_the_scale_and_budgets_hold_the_operator_order():
    assert ledger_load.TASKS >= 1000
    assert ledger_load.LEDGER_BYTES >= 5_000_000
    assert ledger_load.CLIENTS >= 10
    assert ledger_load.WRITE_P95_S <= 2.0
    assert ledger_load.CPU_CORES <= 1.0


def test_the_stored_ledger_exports_as_generated_with_a_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_load, "TASKS", 20)
    monkeypatch.setattr(ledger_load, "LEDGER_BYTES", 0)
    doc = ledger_load.full_size(1)
    token = ledger_load.store(tmp_path, doc)
    exported = SQLiteLedgerRepository(tmp_path / DATABASE).export_document(ledger_load.SLUG)
    assert token
    assert len(exported["tasks"]) == 20
    assert exported["alerts"] == doc["alerts"]


def test_the_clients_together_write_headroom_times_the_live_peak_minute():
    per_minute = ledger_load.CLIENTS * 60 / ledger_load.write_every()
    assert per_minute == pytest.approx(ledger_load.LIVE_PEAK_WRITES_PER_MINUTE * ledger_load.HEADROOM)
    assert ledger_load.HEADROOM >= 2


def test_every_client_reads_one_single_resource_a_second_that_the_ledger_holds():
    doc = ledger_load.document(0)
    held = {f"{name}/{item['id']}" for name in ("tasks", "phases", "followups") for item in doc[name]}
    items = [ledger_load.single_items(n) for n in range(ledger_load.CLIENTS)]
    assert ledger_load.READS_PER_SECOND == 1.0
    assert items[1] == ["tasks/t100", "phases/p1", "followups/f-1"]
    assert all(set(paths) <= held for paths in items)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Api:
    def mutate(self, slug, ops):
        return {}

    def request(self, slug, path):
        return {}


def test_a_client_stops_at_the_deadline_without_sleeping_toward_a_write_it_will_not_send(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ledger_load, "time", clock)
    results = {"writes": [], "reads": [], "errors": []}
    deadline = 2.5 * ledger_load.write_every()
    ledger_load.client(Api(), "ci@ab0000-0000", "tasks/t0", 0.0, deadline, results)
    assert (len(results["writes"]), len(results["reads"]), results["errors"]) == (3, 3, [])
    assert clock.now <= deadline


def test_a_reader_stops_at_the_deadline_without_sleeping_toward_a_read_it_will_not_send(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ledger_load, "time", clock)
    results = {"item_reads": [], "errors": []}
    ledger_load.reader(Api(), ledger_load.single_items(0), 0.0, 2.5, results)
    assert (len(results["item_reads"]), results["errors"]) == (3, [])
    assert clock.now <= 2.5


class EventStream:
    def __init__(self, clock, chunks):
        self.clock = clock
        self.chunks = iter(chunks)
        self.timeout = ledger_load.TIMEOUT_S
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self))
        self.closed = False

    def settimeout(self, seconds):
        self.timeout = seconds

    def read1(self, count):
        delay, chunk = next(self.chunks)
        self.clock.sleep(min(delay, self.timeout))
        if delay > self.timeout:
            raise TimeoutError("silent event stream")
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def test_the_event_stream_stops_at_the_window_end_without_waiting_for_the_next_heartbeat(monkeypatch):
    clock = Clock()
    stream = EventStream(clock, [(0.0, b"heartbeat"), (ledger_load.TIMEOUT_S, b"heartbeat")])
    monkeypatch.setattr(ledger_load, "time", clock)
    monkeypatch.setattr(ledger_load.urllib.request, "urlopen", lambda *args, **kwargs: stream)
    results = {"errors": []}
    ledger_load.watcher("http://localhost", {}, 2.5, results)
    assert clock.now == 2.5
    assert results["errors"] == []
    assert stream.closed


def test_a_silent_event_stream_inside_the_window_stays_red(monkeypatch):
    clock = Clock()
    stream = EventStream(clock, [(0.0, b"heartbeat"), (ledger_load.TIMEOUT_S + 1, b"heartbeat")])
    monkeypatch.setattr(ledger_load, "time", clock)
    monkeypatch.setattr(ledger_load.urllib.request, "urlopen", lambda *args, **kwargs: stream)
    results = {"errors": []}
    ledger_load.watcher("http://localhost", {}, ledger_load.TIMEOUT_S * 2, results)
    assert clock.now == ledger_load.TIMEOUT_S
    assert results["errors"] == ["event stream: TimeoutError: silent event stream"]
    assert stream.closed
