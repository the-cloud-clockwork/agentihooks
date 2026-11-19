import json
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import yaml

from hooks.context import blocker_window, master_coding
from hooks.observability import otel
from scripts.swarm import cli, panic
from scripts.swarm.health import spawn_stall
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig
from scripts.swarm.tick_watch import STALE_TICK, _alert
from scripts.tick.adapters.fakes import Call, FakeSourceControl
from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import ChangeRef, ChangeState, RepositoryRun

pytestmark = pytest.mark.unit

SLUG = "sw"
SID = "master-session"
HOUR = panic.HOUR_MS
WINDOW = panic.WINDOW_MS
FIX = "https://github.com/the-cloud-clockwork/agentihooks/pull/1"
BRANCH_FIX = "https://github.com/the-cloud-clockwork/agentihooks/tree/fix-branch"
HELD_S = 5.0
CODE = "a1b2c3"
MINE = f"engineer-{CODE}-0001"
NO_LAB = "lab not told: the swarm has no lab url"


def _owns_all(branch: str) -> bool:
    """Own every branch, so a sweep test sees only the fix, queue and release rules."""
    return True


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    found = RedisStore(redis)
    found.names.mint = lambda: CODE
    found.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: redis)
    return found


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float, dict[str, str]]]:
    calls: list[tuple[str, float, dict[str, str]]] = []
    monkeypatch.setattr(otel, "add_counter", lambda name, value, attrs: calls.append((name, value, attrs)))
    return calls


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the session the panic guard sees to the panicking swarm."""
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)


@pytest.fixture(autouse=True)
def no_sweep_left() -> Iterator[None]:
    yield
    _sweep_ended()


class Runs:
    def __init__(self, rows: dict[str, list[RepositoryRun]]) -> None:
        self.rows = rows
        self.made: dict[str, FakeSourceControl] = {}

    def __call__(self, repository: str) -> FakeSourceControl:
        answers: dict[str, object] = {"latest_runs": tuple(self.rows.get(repository, []))}
        return self.made.setdefault(repository, FakeSourceControl(answers))

    def calls(self) -> list[tuple[str, Call]]:
        return [(repository, call) for repository, port in self.made.items() for call in port.calls]

    def cancelled(self) -> list[str]:
        return [f"{repository}:{call.args[0]}" for repository, call in self.calls() if call.operation == "cancel_run"]


class Broken(FakeSourceControl):
    def latest_runs(self, *, deadline: Deadline) -> tuple[RepositoryRun, ...]:
        raise subprocess.CalledProcessError(1, ["listing"])

    def cancel_run(self, run_id: int, *, deadline: Deadline) -> None:
        pytest.fail("a failed listing reached the cancel")


class Unread(FakeSourceControl):
    def download(self, path: str, *, deadline: Deadline) -> bytes:
        raise subprocess.CalledProcessError(1, ["gh", "api", path], stderr="HTTP 404\n")


def _branches(monkeypatch: pytest.MonkeyPatch, branches: dict[str, str]) -> list[str]:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    asked: list[str] = []

    def change(url: str, refused: str) -> ChangeState:
        assert refused == f"cannot read pull request {url}"
        asked.append(url)
        owner, name = url.removeprefix("https://github.com/").split("/")[:2]
        return ChangeState(ChangeRef(f"{owner}/{name}", 1), "open", "a" * 40, "dev", False, url, branches[url])

    monkeypatch.setattr(stack, "change", change)
    return asked


def _heads(monkeypatch: pytest.MonkeyPatch, head: str | None) -> list[str]:
    """Answer every Actions run or job read with ``head`` as its head branch and return the API paths read."""
    read: list[str] = []

    class Port(FakeSourceControl):
        def download(self, path: str, *, deadline: Deadline) -> bytes:
            read.append(path)
            return json.dumps({"head_branch": head}).encode()

    monkeypatch.setattr(panic, "port", lambda repository: Port())
    return read


def _row(run_id: int, branch: str, status: str = "in_progress", event: str = "pull_request") -> RepositoryRun:
    return RepositoryRun(run_id, branch, event, status)


def _errors(store: RedisStore, count: int, at: int, category: str = "SpawnError") -> None:
    for _ in range(count):
        panic.count_error(store, SLUG, category, at)


def _passes(store: RedisStore, ok: int, bad: int, at: int) -> None:
    for _ in range(ok):
        panic.count_pass(store, SLUG, True, at)
    for _ in range(bad):
        panic.count_pass(store, SLUG, False, at)


def _sweep_ended() -> None:
    assert panic.SWEEP.acquire(timeout=HELD_S)
    panic.SWEEP.release()


def _tick(store: RedisStore, ok: bool, now_ms: int, ports: panic.Ports) -> list[str]:
    reported: list[str] = []
    actions = panic.tick_pass(store, SLUG, ok, now_ms, reported.append, ports)
    _sweep_ended()
    return [*actions, *reported]


class Held(FakeSourceControl):
    def __init__(self, repository: str, gate: threading.Event, listed: list[str]) -> None:
        super().__init__()
        self.repository, self.gate, self.listed = repository, gate, listed

    def latest_runs(self, *, deadline: Deadline) -> tuple[RepositoryRun, ...]:
        self.listed.append(self.repository)
        if self.repository == panic.REPOS[0]:
            self.gate.wait(HELD_S)
        return (_row(1, MINE),)


def _held(gate: threading.Event, listed: list[str]) -> panic.Ports:
    return lambda repository: Held(repository, gate, listed)


def test_an_error_lands_on_the_otel_counter_by_swarm_and_category(store, counted):
    panic.count_error(store, SLUG, "SpawnError", 1_000)
    assert counted == [(panic.METRIC, 1, {"swarm": SLUG, "category": "SpawnError"})]
    assert panic.METRIC == "agentihooks.swarm.errors"


def test_the_counter_reads_errors_in_the_last_hour_by_category(store, counted):
    now = 10 * HOUR
    _errors(store, 2, now - HOUR, "SpawnError")
    _errors(store, 1, now - HOUR - 1, "SpawnError")
    _errors(store, 3, now, STALE_TICK)
    assert panic.errors_since(store, SLUG, now - HOUR) == 5
    assert panic.errors_by_category(store, SLUG, now - HOUR) == {"SpawnError": 2, STALE_TICK: 3}


def test_the_window_drops_errors_older_than_an_hour(store, counted):
    _errors(store, 1, 0)
    _errors(store, 1, HOUR + 1)
    assert store.redis.zcard(f"{PREFIX}:{SLUG}:panic:errors") == 1


def test_otel_add_counter_creates_one_counter_and_adds(monkeypatch):
    added: list[tuple[float, dict[str, str]]] = []

    class Counter:
        def add(self, value: float, attrs: dict[str, str]) -> None:
            added.append((value, attrs))

    class Meter:
        made = 0

        def create_counter(self, name: str) -> Counter:
            assert name == "c"
            Meter.made += 1
            return Counter()

    monkeypatch.setattr(otel, "_meter", Meter())
    monkeypatch.setattr(otel, "_counters", {})
    otel._dispatch_op(("counter", "c", 1.0, {"swarm": "sw"}))
    otel._dispatch_op(("counter", "c", 2.0, {"swarm": "sw"}))
    assert added == [(1.0, {"swarm": "sw"}), (2.0, {"swarm": "sw"})]
    assert Meter.made == 1


def test_otel_add_counter_without_a_meter_counts_unsupported(monkeypatch):
    monkeypatch.setattr(otel, "_meter", None)
    monkeypatch.setattr(otel, "_counts", {})
    otel._dispatch_op(("counter", "c", 1.0, {"session.id": "s"}))
    assert otel._counts == {("s", "counters", "unsupported"): 1}


def test_add_counter_enqueues_a_counter_op(monkeypatch):
    queued: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(otel, "_enqueue", lambda signal, op: queued.append((signal, op)))
    otel.add_counter("c", 2, {"swarm": "sw"})
    otel.add_counter("d", 1)
    assert queued == [("counters", ("counter", "c", 2.0, {"swarm": "sw"})), ("counters", ("counter", "d", 1.0, {}))]


def test_a_failed_tick_step_counts_an_error(store, counted, monkeypatch):
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "findings", lambda *args: [])
    monkeypatch.setattr(panic, "tick_pass", lambda *args: [])
    from scripts.swarm import timing

    with spawn_stall.watch(store, SLUG, None, lambda: 5_000, None):
        failed = timing.ON_FAILURE.get()
        assert failed is not None
        failed("step", RuntimeError("boom"))
    assert panic.errors_by_category(store, SLUG, 0) == {"RuntimeError": 1}
    assert counted == [(panic.METRIC, 1, {"swarm": SLUG, "category": "RuntimeError"})]


def test_a_tick_pass_records_whether_any_step_failed(store, counted, monkeypatch, capsys):
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "findings", lambda *args: [])
    seen: list[tuple[str, bool, int]] = []
    monkeypatch.setattr(panic, "tick_pass", lambda store, slug, ok, now, report: seen.append((slug, ok, now)) or ["x"])
    from scripts.swarm import timing

    with spawn_stall.watch(store, SLUG, None, lambda: 7_000, None):
        pass
    with spawn_stall.watch(store, SLUG, None, lambda: 8_000, None):
        failed = timing.ON_FAILURE.get()
        assert failed is not None
        failed("step", RuntimeError("boom"))
    assert seen == [(SLUG, True, 7_000), (SLUG, False, 8_000)]
    line = '{"event": "swarm_panic", "slug": "sw", "action": "x"}'
    assert [row for row in capsys.readouterr().out.splitlines() if "swarm_panic" in row] == [line, line]


def test_the_pass_window_drops_passes_older_than_fifteen_minutes(store):
    panic.count_pass(store, SLUG, True, 0)
    panic.count_pass(store, SLUG, True, 1)
    panic.count_pass(store, SLUG, True, WINDOW + 1)
    assert store.redis.zcard(f"{PREFIX}:{SLUG}:panic:passes-ok") == 2
    panic.count_pass(store, SLUG, False, 0)
    assert store.redis.zcard(f"{PREFIX}:{SLUG}:panic:passes-failed") == 1


def test_the_error_window_keeps_errors_inside_the_hour(store, counted):
    _errors(store, 1, 1)
    _errors(store, 1, HOUR + 1)
    assert store.redis.zcard(f"{PREFIX}:{SLUG}:panic:errors") == 2


def test_due_cancel_holds_the_cancel_key_for_a_minute(store):
    assert panic.due_cancel(store, SLUG, 0) is True
    assert panic.due_cancel(store, SLUG, 1) is False
    ttl = store.redis.pttl(f"{PREFIX}:{SLUG}:panic:cancel")
    assert 0 < ttl <= panic.CANCEL_EVERY_MS


def test_a_stale_tick_counts_an_error(store, counted):
    _alert(store, SLUG, 10_000, None, ["late"])
    assert panic.errors_by_category(store, SLUG, 0) == {STALE_TICK: 1}
    assert counted == [(panic.METRIC, 1, {"swarm": SLUG, "category": STALE_TICK})]


def test_the_alarm_engages_panic_at_the_threshold(store, counted):
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD - 1, now)
    assert panic.alarm(store, SLUG, now) == ""
    assert panic.state(store, SLUG) is None
    _errors(store, 1, now)
    assert panic.alarm(store, SLUG, now) == f"panic engaged: {panic.THRESHOLD} errors in the last hour"
    assert panic.state(store, SLUG) == panic.Panic(now, f"{panic.THRESHOLD} errors in the last hour", True, ())


def test_the_threshold_is_sixty_errors_an_hour():
    assert panic.THRESHOLD == 60
    assert panic.RELEASE_LEVEL == 0.9
    assert panic.RELEASE_ERRORS == 15


def test_the_alarm_counts_only_the_last_hour(store, counted):
    now = 3 * HOUR
    _errors(store, panic.THRESHOLD, now - HOUR - 1)
    assert panic.alarm(store, SLUG, now) == ""


def test_an_automatic_panic_releases_once_stable(store, counted):
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD, now)
    panic.alarm(store, SLUG, now)
    later = now + WINDOW + 1
    _passes(store, 9, 1, later)
    assert panic.alarm(store, SLUG, later) == "panic released: the fleet is back at business as usual"
    assert panic.state(store, SLUG) is None


def test_an_automatic_panic_holds_below_ninety_percent(store, counted):
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD, now)
    panic.alarm(store, SLUG, now)
    later = now + WINDOW + 1
    _passes(store, 8, 2, later)
    assert panic.level(store, SLUG, later) == 0.8
    assert panic.alarm(store, SLUG, later) == ""
    assert panic.state(store, SLUG) is not None


def test_a_manual_panic_never_releases_itself(store, counted):
    panic.engage(store, SLUG, "pressed", False, [FIX], 0)
    _passes(store, 10, 0, WINDOW + 1)
    assert panic.alarm(store, SLUG, WINDOW + 1) == ""
    assert panic.state(store, SLUG) == panic.Panic(0, "pressed", False, (FIX,))


def test_stable_needs_the_level_and_few_recent_errors(store, counted):
    now = 5 * HOUR
    _passes(store, 9, 1, now)
    _errors(store, panic.RELEASE_ERRORS - 1, now - WINDOW)
    assert panic.stable(store, SLUG, now) is True
    _errors(store, 1, now)
    assert panic.stable(store, SLUG, now) is False


def test_level_is_one_with_no_passes_and_ignores_old_ones(store):
    assert panic.level(store, SLUG, HOUR) == 1.0
    _passes(store, 0, 3, HOUR - WINDOW - 1)
    _passes(store, 1, 0, HOUR - WINDOW)
    assert panic.level(store, SLUG, HOUR) == 1.0


def test_engage_keeps_the_first_panic_and_adds_fixes(store: RedisStore) -> None:
    panic.engage(store, SLUG, "auto", True, [], 100)
    found = panic.engage(store, SLUG, "pressed", False, [FIX, FIX], 200)
    assert found == panic.Panic(100, "auto", True, (FIX,))
    assert panic.state(store, SLUG) == found


def test_a_fix_added_to_an_automatic_panic_still_releases_once_stable(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]]
) -> None:
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD, now)
    panic.alarm(store, SLUG, now)
    panic.engage(store, SLUG, "pressed", False, (fix for fix in [FIX]), now + 1)
    assert panic.state(store, SLUG) == panic.Panic(now, f"{panic.THRESHOLD} errors in the last hour", True, (FIX,))
    later = now + WINDOW + 1
    _passes(store, 9, 1, later)
    assert panic.alarm(store, SLUG, later) == "panic released: the fleet is back at business as usual"
    assert panic.state(store, SLUG) is None


def test_pressing_on_without_a_fix_makes_an_automatic_panic_manual(store: RedisStore) -> None:
    panic.engage(store, SLUG, "auto", True, [], 100)
    assert panic.engage(store, SLUG, "pressed", False, [], 200) == panic.Panic(100, "auto", False, ())


def test_a_panic_a_person_turns_on_without_a_fix_stays_manual(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]]
) -> None:
    panic.engage(store, SLUG, "pressed", False, [], 0)
    _passes(store, 10, 0, WINDOW + 1)
    assert panic.alarm(store, SLUG, WINDOW + 1) == ""
    assert panic.state(store, SLUG) == panic.Panic(0, "pressed", False, ())


def test_engage_on_an_automatic_panic_stays_automatic(store):
    panic.engage(store, SLUG, "auto", True, [], 100)
    assert panic.engage(store, SLUG, "again", True, ["b", "a"], 200) == panic.Panic(100, "auto", True, ("a", "b"))


def test_release_refuses_below_the_level(store, counted):
    panic.engage(store, SLUG, "pressed", False, [], 0)
    _passes(store, 1, 1, HOUR)
    assert panic.release(store, SLUG, HOUR) is False
    assert panic.state(store, SLUG) is not None


def test_cancel_runs_spares_the_merge_queue_the_fix_and_finished_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    fix = "https://github.com/o/a/pull/1"
    asked = _branches(monkeypatch, {fix: "fix-branch"})
    runs = Runs(
        {
            "o/a": [
                _row(1, "feature"),
                _row(2, "fix-branch"),
                _row(3, "gh-readonly-queue/dev/pr-9", event="merge_group"),
                _row(4, "done", status="completed"),
                _row(5, "waiting", status="queued"),
            ],
            "o/b": [_row(6, "other", status="pending"), _row(7, "fix-branch")],
        },
    )
    cancelled = panic.cancel_runs([fix], ["o/a", "o/b"], runs, _owns_all)
    assert cancelled == [
        "o/a run 1 on feature",
        "o/a run 5 on waiting",
        "o/b run 6 on other",
        "o/b run 7 on fix-branch",
    ]
    assert [(repository, call.operation, call.args) for repository, call in runs.calls()] == [
        ("o/a", "latest_runs", ()),
        ("o/a", "cancel_run", (1,)),
        ("o/a", "cancel_run", (5,)),
        ("o/b", "latest_runs", ()),
        ("o/b", "cancel_run", (6,)),
        ("o/b", "cancel_run", (7,)),
    ]
    assert all(panic.TOOL_SECONDS - 5 < call.deadline.remaining <= panic.TOOL_SECONDS for _, call in runs.calls())
    assert panic.TOOL_SECONDS == 60.0
    assert asked == [fix]


def test_every_active_status_is_cancelled():
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        runs = Runs({"o/a": [_row(1, "x", status=status)]})
        assert panic.cancel_runs([], ["o/a"], runs, _owns_all) == ["o/a run 1 on x"]


def test_cancel_runs_spares_release_tag_runs() -> None:
    runs = Runs(
        {
            "o/a": [
                _row(1, "v2.17.652", event="release"),
                _row(2, "v2.17.653", event="push"),
                _row(3, "v2-feature"),
                _row(4, "release", event="push"),
                _row(5, "main", event="release"),
            ],
        },
    )
    assert panic.cancel_runs([], ["o/a"], runs, _owns_all) == ["o/a run 3 on v2-feature", "o/a run 4 on release"]
    assert runs.cancelled() == ["o/a:3", "o/a:4"]


@pytest.mark.feature("panic-merged-fix-dev-run")
def test_cancel_runs_spares_the_dev_run_of_a_merged_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    merged = "m" * 40

    def change(url: str, refused: str) -> ChangeState:
        return ChangeState(ChangeRef("o/r", 1), "closed", "a" * 40, "dev", True, url, "fix-branch", merge_commit=merged)

    monkeypatch.setattr(stack, "change", change)
    runs = Runs(
        {
            "o/a": [
                RepositoryRun(1, "dev", "push", "in_progress", head_sha=merged),
                RepositoryRun(2, "dev", "push", "in_progress", head_sha="b" * 40),
                RepositoryRun(3, "feature", "pull_request", "queued", head_sha=""),
            ],
        },
    )
    assert panic.cancel_runs([FIX], ["o/a"], runs, _owns_all) == ["o/a run 2 on dev", "o/a run 3 on feature"]


@pytest.mark.feature("panic-merged-fix-dev-run")
def test_cancel_runs_cancels_the_run_on_an_open_fix_merge_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    commit = "m" * 40

    def change(url: str, refused: str) -> ChangeState:
        return ChangeState(ChangeRef("o/r", 1), "open", "a" * 40, "dev", False, url, "fix-branch", merge_commit=commit)

    monkeypatch.setattr(stack, "change", change)
    runs = Runs({"o/a": [RepositoryRun(1, "dev", "push", "in_progress", head_sha=commit)]})
    assert panic.cancel_runs([FIX], ["o/a"], runs, _owns_all) == ["o/a run 1 on dev"]


def _fix_reads(monkeypatch: pytest.MonkeyPatch, merged: set[str], commit: str) -> list[str]:
    """Answer every fix pull request read with a state merged at ``commit`` for the urls in ``merged``, open for the
    rest, and return the urls in the order they were read."""
    from scripts.swarm import stack

    reads: list[str] = []

    def change(url: str, refused: str) -> ChangeState:
        reads.append(url)
        return _fix(url, url in merged, commit)

    monkeypatch.setattr(stack, "change", change)
    return reads


def _fix(url: str, merged: bool, commit: str) -> ChangeState:
    """Return the state of fix pull request ``url`` of o/a, merged at ``commit`` when ``merged``, else open."""
    number = int(url.rsplit("/", 1)[1])
    state = "closed" if merged else "open"
    return ChangeState(
        ChangeRef("o/a", number), state, "a" * 40, "dev", merged, url, f"fix-{number}", merge_commit=commit
    )


@pytest.mark.feature("panic-sweep-keeps-merged-fixes")
def test_tick_pass_reads_a_merged_fix_once_and_an_open_fix_every_sweep(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    merged, opened, commit = FIX, f"{FIX}0", "m" * 40
    reads = _fix_reads(monkeypatch, {merged}, commit)
    monkeypatch.setattr(panic, "due_cancel", lambda *_: True)
    rows = [RepositoryRun(1, MINE, "push", "in_progress", head_sha=commit), _row(2, f"{MINE}-2")]
    runs = Runs({repo: rows for repo in panic.REPOS})
    panic.engage(store, SLUG, "pressed", False, [merged, opened], 0)
    sweeps = [_tick(store, True, at, runs) for at in (1_000, 1_000 + panic.CANCEL_EVERY_MS)]
    assert reads == [merged, opened, opened]
    assert sweeps == [[f"panic cancelled {repo} run 2 on {MINE}-2" for repo in panic.REPOS]] * 2
    assert panic.fix_state((store, SLUG), merged) == _fix(merged, True, commit)
    assert reads == [merged, opened, opened]


@pytest.mark.feature("panic-sweep-keeps-merged-fixes")
def test_cancel_runs_without_a_store_reads_every_fix_every_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _fix_reads(monkeypatch, {FIX}, "m" * 40)
    for _ in range(2):
        panic.cancel_runs([FIX], ["o/a"], Runs({}), _owns_all)
    assert reads == [FIX, FIX]


@pytest.mark.feature("panic-sweep-keeps-merged-fixes")
def test_a_merged_fix_is_read_again_after_panic_releases(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _fix_reads(monkeypatch, {FIX}, "m" * 40)
    monkeypatch.setattr(panic, "due_cancel", lambda *_: True)
    panic.engage(store, SLUG, "pressed", False, [FIX], 0)
    for at in (1_000, 2_000):
        _tick(store, True, at, Runs({}))
    assert reads == [FIX]
    assert panic.release(store, SLUG, 2_000)
    panic.engage(store, SLUG, "pressed", False, [FIX], 3_000)
    _tick(store, True, 3_000, Runs({}))
    assert reads == [FIX, FIX]


@pytest.mark.parametrize("drift", ["state", "reference"])
@pytest.mark.feature("panic-sweep-keeps-merged-fixes")
def test_a_kept_fix_row_of_another_shape_is_read_again(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    commit = "m" * 40
    reads = _fix_reads(monkeypatch, {FIX}, commit)
    stale = asdict(_fix(FIX, True, commit))
    if drift == "state":
        del stale["body"]
    else:
        stale["reference"]["owner"] = "o"
    store.redis.hset(f"{PREFIX}:{SLUG}:panic:{panic.MERGED_FIXES}", FIX, json.dumps(stale))
    assert panic.fix_state((store, SLUG), FIX) == _fix(FIX, True, commit)
    assert panic.fix_state((store, SLUG), FIX) == _fix(FIX, True, commit)
    assert reads == [FIX]


@pytest.mark.feature("panic-merged-fix-integration-head")
def test_cancel_runs_spares_the_integration_head_of_a_merged_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    def change(url: str, refused: str) -> ChangeState:
        return ChangeState(
            ChangeRef("o/a", 1), "closed", "a" * 40, "dev", True, url, "fix-branch", merge_commit="m" * 40
        )

    monkeypatch.setattr(stack, "change", change)
    runs = Runs(
        {
            "o/a": [
                RepositoryRun(1, "dev", "workflow_dispatch", "in_progress", head_sha="d" * 40),
                RepositoryRun(9, "dev", "push", "in_progress", head_sha=""),
                RepositoryRun(2, "dev", "push", "in_progress", head_sha="n" * 40),
                RepositoryRun(3, "dev", "push", "queued", head_sha="n" * 40),
                RepositoryRun(4, "dev", "push", "in_progress", head_sha="m" * 40),
                RepositoryRun(5, "dev", "push", "in_progress", head_sha="o" * 40),
                RepositoryRun(6, "feature", "push", "in_progress", head_sha="f" * 40),
                RepositoryRun(7, "other", "pull_request", "queued", head_sha="n" * 40),
            ],
            "o/b": [RepositoryRun(8, "dev", "push", "in_progress", head_sha="p" * 40)],
        },
    )
    assert panic.cancel_runs([FIX], ["o/a", "o/b"], runs, _owns_all) == [
        "o/a run 1 on dev",
        "o/a run 9 on dev",
        "o/a run 5 on dev",
        "o/a run 6 on feature",
        "o/b run 8 on dev",
    ]


@pytest.mark.feature("panic-merged-fix-integration-head")
def test_cancel_runs_spares_the_integration_head_of_each_merged_fix_base(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    bases = {FIX: ("dev", 1), f"{FIX}0": ("hotfix", 10)}

    def change(url: str, refused: str) -> ChangeState:
        base, number = bases[url]
        return ChangeState(ChangeRef("o/a", number), "closed", "a" * 40, base, True, url, f"fix-{number}")

    monkeypatch.setattr(stack, "change", change)
    runs = Runs(
        {
            "o/a": [
                RepositoryRun(1, "hotfix", "push", "in_progress", head_sha="h" * 40),
                RepositoryRun(2, "dev", "push", "in_progress", head_sha="n" * 40),
                RepositoryRun(3, "hotfix", "push", "in_progress", head_sha="g" * 40),
                RepositoryRun(4, "dev", "push", "in_progress", head_sha="o" * 40),
            ],
        },
    )
    assert panic.cancel_runs([FIX, f"{FIX}0"], ["o/a"], runs, _owns_all) == ["o/a run 3 on hotfix", "o/a run 4 on dev"]


@pytest.mark.feature("panic-merged-fix-integration-head")
def test_cancel_runs_spares_the_queue_merge_of_a_fix_before_it_reads_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    def change(url: str, refused: str) -> ChangeState:
        return ChangeState(ChangeRef("o/a", 7), "open", "a" * 40, "dev", False, url, "fix-branch")

    monkeypatch.setattr(stack, "change", change)
    runs = Runs(
        {
            "o/a": [
                RepositoryRun(1, "dev", "push", "in_progress", head_sha="q" * 40),
                RepositoryRun(2, "gh-readonly-queue/dev/pr-7-abc", "merge_group", "completed", head_sha="q" * 40),
                RepositoryRun(3, "gh-readonly-queue/dev/pr-70-abc", "merge_group", "completed", head_sha="r" * 40),
                RepositoryRun(4, "dev", "push", "in_progress", head_sha="r" * 40),
                RepositoryRun(5, "gh-readonly-queue/main/pr-7-abc", "merge_group", "completed", head_sha="s" * 40),
                RepositoryRun(6, "main", "push", "in_progress", head_sha="s" * 40),
                RepositoryRun(7, "gh-readonly-queue/dev/pr-7-abc", "pull_request", "completed", head_sha="t" * 40),
                RepositoryRun(8, "dev", "push", "in_progress", head_sha="t" * 40),
                RepositoryRun(9, None, "push", "in_progress", head_sha=""),
                RepositoryRun(12, "gh-readonly-queue/dev/pr-7-def", "merge_group", "completed", head_sha=""),
            ],
            "o/b": [
                RepositoryRun(10, "gh-readonly-queue/dev/pr-7-abc", "merge_group", "completed", head_sha="u" * 40),
                RepositoryRun(11, "dev", "push", "in_progress", head_sha="u" * 40),
            ],
        },
    )
    assert panic.cancel_runs([FIX], ["o/a", "o/b"], runs, _owns_all) == [
        "o/a run 4 on dev",
        "o/a run 6 on main",
        "o/a run 8 on dev",
        "o/a run 9 on None",
        "o/b run 11 on dev",
    ]


def _github_runs(monkeypatch: pytest.MonkeyPatch, rows: dict[str, list[dict[str, object]]]) -> None:
    from scripts.tick.adapters import github

    def rest(caller: str, args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        repository, found, _ = args[1].removeprefix("repos/").partition("/actions/runs?")
        body = json.dumps({"workflow_runs": rows[repository]}) if found else ""
        return subprocess.CompletedProcess(args, 0, body, "")

    monkeypatch.setattr(github.github_reads, "rest", rest)


def _api_run(run_id: int, title: str, event: str = "workflow_run") -> dict[str, object]:
    return {"id": run_id, "head_branch": "main", "event": event, "status": "queued", "display_title": title}


@pytest.mark.feature("panic-fix-triggered-workflow-run")
def test_cancel_runs_keeps_the_workflow_run_runs_a_fix_run_triggered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    here, there = "the-cloud-clockwork/agentihooks", "the-cloud-clockwork/antoncore"
    _github_runs(
        monkeypatch,
        {
            here: [
                _api_run(1, "Gate Required status for fix-branch"),
                _api_run(2, "Gate Required status for hotfix"),
                _api_run(3, "Gate Required status for feature"),
                _api_run(4, "Gate Required status"),
                _api_run(5, "Gate Required status for fix-branch-2"),
                _api_run(6, "Cancel the push run for fix-branch", event="push"),
            ],
            there: [
                _api_run(7, "Gate Required status for hotfix"),
                _api_run(8, "Gate Required status for fix-branch"),
            ],
        },
    )
    fixes = [FIX, f"https://github.com/{here}/tree/hotfix"]
    assert panic.cancel_runs(
        fixes, [here, there], lambda repo: GitHubSourceControl(repo, tmp_path, "dev"), _owns_all
    ) == [
        f"{here} run 3 on main",
        f"{here} run 4 on main",
        f"{here} run 5 on main",
        f"{here} run 6 on main",
        f"{there} run 7 on main",
        f"{there} run 8 on main",
    ]


@pytest.mark.feature("panic-fix-triggered-workflow-run")
def test_the_gate_required_run_name_names_the_branch_the_sweep_keeps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/gate-required.yml").read_text())
    branch = "${{ github.event.workflow_run.head_branch }}"
    name = workflow.get("run-name")
    assert name == f"Gate Required status for {branch}"
    titles = [name.replace(branch, fix) for fix in ("fix-branch", "feature")]
    here = "the-cloud-clockwork/agentihooks"
    _github_runs(monkeypatch, {here: [_api_run(number, title) for number, title in enumerate(titles, 1)]})
    assert panic.cancel_runs([FIX], [here], lambda repo: GitHubSourceControl(repo, tmp_path, "dev"), _owns_all) == [
        f"{here} run 2 on main"
    ]


def test_the_fleet_repos_include_hooks_bundle_and_antoncore():
    assert panic.REPOS == (
        "the-cloud-clockwork/agentihooks",
        "the-cloud-clockwork/agentihooks-bundle",
        "the-cloud-clockwork/antoncore",
    )


def test_port_answers_the_repository_through_the_source_control_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert panic.port("o/a") == GitHubSourceControl("o/a", tmp_path, "dev")


def test_tick_pass_cancels_once_a_minute_while_panic_holds(store, counted):
    runs = Runs({repo: [_row(1, "engineer-a1b2c3-0001")] for repo in panic.REPOS})
    panic.engage(store, SLUG, "pressed", False, [], 0)
    first = _tick(store, True, 1_000, runs)
    assert first == [f"panic cancelled {repo} run 1 on engineer-a1b2c3-0001" for repo in panic.REPOS]
    assert _tick(store, True, 1_000 + panic.CANCEL_EVERY_MS - 1, runs) == []
    assert len(runs.cancelled()) == len(panic.REPOS)


def test_tick_pass_without_panic_cancels_nothing(store, counted):
    runs = Runs({repo: [_row(1, "engineer-a1b2c3-0001")] for repo in panic.REPOS})
    assert _tick(store, True, 1_000, runs) == []
    assert runs.calls() == []
    assert panic.level(store, SLUG, 1_000) == 1.0


def test_tick_pass_engages_and_cancels_in_one_pass(store, counted):
    _errors(store, panic.THRESHOLD, 1_000)
    runs = Runs({})
    assert _tick(store, False, 1_000, runs) == [f"panic engaged: {panic.THRESHOLD} errors in the last hour"]
    assert panic.level(store, SLUG, 1_000) == 0.0


def test_tick_pass_reports_a_failed_cancel(store, counted):
    panic.engage(store, SLUG, "pressed", False, [], 0)

    found = _tick(store, True, 1_000, lambda repository: Broken())
    assert len(found) == 1
    assert found[0].startswith("panic cancel failed: ")


def test_tick_pass_reports_an_unreadable_fix_branch(store, counted, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.swarm.store import SwarmError

    panic.engage(store, SLUG, "pressed", False, [FIX], 0)

    def unreadable(url: str, refused: str) -> None:
        raise SwarmError(f"{refused}: boom")

    monkeypatch.setattr(stack, "change", unreadable)
    assert _tick(store, True, 1_000, Runs({})) == [f"panic cancel failed: cannot read pull request {FIX}: boom"]


@pytest.mark.parametrize(
    ("fix", "named"),
    [
        (BRANCH_FIX, ("github.com/the-cloud-clockwork/agentihooks", "fix-branch")),
        (
            "https://github.com/The-Cloud-Clockwork/agentihooks/tree/Fix",
            ("github.com/the-cloud-clockwork/agentihooks", "Fix"),
        ),
        ("https://github.com/o/r/tree/fix/nested", ("github.com/o/r", "fix/nested")),
        ("https://github.com/o/tree/tree/b", ("github.com/o/tree", "b")),
        (FIX, ("", "")),
        ("o/r/tree/b", ("", "")),
        ("https://github.com/o/r/tree", ("", "")),
        ("https://github.com/o/r/tree/", ("", "")),
        ("https://github.com//r/tree/b", ("", "")),
    ],
)
@pytest.mark.feature("panic-fix-branch-first-push")
def test_a_branch_link_names_its_repository_and_branch(fix: str, named: tuple[str, str]) -> None:
    assert panic.branch_fix(fix) == named


@pytest.mark.feature("panic-fix-branch-first-push")
def test_tick_pass_spares_a_registered_fix_branch_only_in_its_repository(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed, headed = f"{MINE}-2", f"{MINE}-3"
    asked = _branches(monkeypatch, {FIX: headed})
    runs = Runs({repo: [_row(1, MINE), _row(2, fixed), _row(3, headed)] for repo in panic.REPOS})
    branch_fix = f"https://github.com/the-cloud-clockwork/agentihooks/tree/{fixed}"
    panic.engage(store, SLUG, "pressed", False, [FIX, branch_fix], 0)
    assert _tick(store, True, 1_000, runs) == [
        f"panic cancelled the-cloud-clockwork/agentihooks run 1 on {MINE}",
        f"panic cancelled the-cloud-clockwork/agentihooks-bundle run 1 on {MINE}",
        f"panic cancelled the-cloud-clockwork/agentihooks-bundle run 2 on {fixed}",
        f"panic cancelled the-cloud-clockwork/agentihooks-bundle run 3 on {headed}",
        f"panic cancelled the-cloud-clockwork/antoncore run 1 on {MINE}",
        f"panic cancelled the-cloud-clockwork/antoncore run 2 on {fixed}",
        f"panic cancelled the-cloud-clockwork/antoncore run 3 on {headed}",
    ]
    assert asked == [FIX]


@pytest.mark.feature("panic-sweep-off-pass-thread")
def test_the_pass_ends_before_the_panic_cancel_sweep_it_starts(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import timing
    from scripts.tick.adapters import github

    gate, reported = threading.Event(), threading.Event()
    held: list[bool] = []
    actions: list[str] = []

    def rest(caller: str, args: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        repository, listing, _ = args[1].removeprefix("repos/").partition("/actions/runs?")
        if listing and repository == panic.REPOS[0]:
            held.append(gate.wait(HELD_S))
        body = json.dumps({"workflow_runs": [{**_api_run(1, "busy"), "head_branch": MINE}]}) if listing else ""
        return subprocess.CompletedProcess(args, 0, body, "")

    def emit(stream: object, line: str) -> None:
        if '"event": "swarm_panic",' in line:
            actions.append(json.loads(line)["action"])
            if len(actions) == len(panic.REPOS):
                reported.set()

    monkeypatch.setattr(github.github_reads, "rest", rest)
    monkeypatch.setattr(timing, "emit", emit)
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "findings", lambda *args: [])
    panic.engage(store, SLUG, "pressed", False, [], 0)
    try:
        with spawn_stall.watch(store, SLUG, None, lambda: 1_000, None):
            pass
        early = list(actions)
    finally:
        gate.set()
    assert early == []
    assert reported.wait(HELD_S)
    assert held == [True]
    assert actions == [f"panic cancelled {repo} run 1 on {MINE}" for repo in panic.REPOS]


@pytest.mark.feature("panic-sweep-off-pass-thread")
def test_a_due_pass_starts_no_sweep_while_an_earlier_one_runs(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = set(threading.enumerate())
    gate = threading.Event()
    listed: list[str] = []
    reported: list[str] = []
    monkeypatch.setattr(panic, "due_cancel", lambda *_: True)
    panic.engage(store, SLUG, "pressed", False, [], 0)
    try:
        assert panic.tick_pass(store, SLUG, True, 1_000, reported.append, _held(gate, listed)) == []
        assert panic.tick_pass(store, SLUG, True, 2_000, reported.append, _held(gate, listed)) == []
        sweeps = [thread for thread in threading.enumerate() if thread.name == "panic-sweep" and thread not in before]
    finally:
        gate.set()
    _sweep_ended()
    assert [thread.daemon for thread in sweeps] == [True]
    assert listed == list(panic.REPOS)
    assert reported == [f"panic cancelled {repo} run 1 on {MINE}" for repo in panic.REPOS]


@pytest.mark.feature("panic-sweep-off-pass-thread")
def test_a_sweep_thread_that_cannot_start_frees_the_sweep(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unstartable(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    reported: list[str] = []
    monkeypatch.setattr(panic, "Thread", Unstartable)
    panic.engage(store, SLUG, "pressed", False, [], 0)
    assert panic.tick_pass(store, SLUG, True, 1_000, reported.append, Runs({})) == [
        "panic cancel failed: can't start new thread"
    ]
    assert panic.SWEEP.acquire(blocking=False)
    panic.SWEEP.release()
    assert reported == []


@pytest.mark.feature("panic-sweep-off-pass-thread")
def test_settle_returns_once_the_running_sweep_reported(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]]
) -> None:
    gate, settled = threading.Event(), threading.Event()
    listed: list[str] = []
    reported: list[str] = []

    def settle() -> None:
        panic.settle()
        settled.set()

    panic.engage(store, SLUG, "pressed", False, [], 0)
    assert panic.tick_pass(store, SLUG, True, 1_000, reported.append, _held(gate, listed)) == []
    waiter = threading.Thread(target=settle)
    waiter.start()
    try:
        assert not settled.wait(0.2)
    finally:
        gate.set()
    assert settled.wait(HELD_S)
    waiter.join(HELD_S)
    assert reported == [f"panic cancelled {repo} run 1 on {MINE}" for repo in panic.REPOS]


@pytest.mark.feature("panic-sweep-off-pass-thread")
@pytest.mark.parametrize(("argv", "code"), [(["panic"], 0), (["panic", "off"], 1)])
def test_a_swarm_command_waits_for_the_running_panic_sweep_before_it_returns(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], argv: list[str], code: int
) -> None:
    gate = threading.Event()
    listed: list[str] = []
    reported: list[str] = []
    _passes(store, 0, 1, cli.now_ms())
    panic.engage(store, SLUG, "pressed", False, [], 0)
    assert panic.tick_pass(store, SLUG, True, 1_000, reported.append, _held(gate, listed)) == []
    opener = threading.Timer(0.2, gate.set)
    opener.start()
    try:
        assert cli.main([SLUG, *argv]) == code
        returned = list(reported)
    finally:
        gate.set()
        opener.cancel()
    assert returned == [f"panic cancelled {repo} run 1 on {MINE}" for repo in panic.REPOS]


@pytest.mark.feature("panic-fix-pull-request-head-in-its-repository")
def test_cancel_runs_spares_a_fix_pull_request_head_only_in_its_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _branches(monkeypatch, {FIX: "fix-branch"})
    runs = Runs({repo: [_row(1, "fix-branch")] for repo in panic.REPOS})
    assert panic.cancel_runs([FIX], panic.REPOS, runs, _owns_all) == [
        "the-cloud-clockwork/agentihooks-bundle run 1 on fix-branch",
        "the-cloud-clockwork/antoncore run 1 on fix-branch",
    ]
    assert runs.cancelled() == ["the-cloud-clockwork/agentihooks-bundle:1", "the-cloud-clockwork/antoncore:1"]
    assert asked == [FIX]


def _fixer_rows() -> list[dict[str, Any]]:
    return [
        {"id": "promo-43992907244-1", "state": "claimed", "claimed_by": "ci@323133-5149", "branch": "ci-323133-5149"},
        {
            "id": "promo-43992907244-0",
            "state": "pr",
            "claimed_by": "ci@323133-5153",
            "branch": "ci-323133-5150",
            "repository_branches": {"/home/hive/dev/antoncore": "ci-323133-5150-2"},
        },
        {"id": "crew-1116073743-fix-1", "state": "claimed", "claimed_by": "engineer@323133-10087"},
        {"id": "crew-1116073743-scout-1", "state": "claimed", "claimed_by": "ci@323133-5140", "branch": "ci-1"},
        {"id": "promo-43913675090-0", "state": "done", "claimed_by": "ci@323133-5084", "branch": "ci-323133-5084"},
        {"id": "t-panic-spares-fix-prs", "state": "claimed", "claimed_by": "ci@323133-5155", "branch": "ci-2"},
        {"id": "promo-1-0", "state": "open", "branch": "dev", "repository_branches": {"/home/hive/dev/x": "main"}},
    ]


def _fixer_links(*branches: str) -> list[str]:
    return sorted(f"https://github.com/{repo}/tree/{branch}" for repo in panic.REPOS for branch in branches)


def _record(store: RedisStore, slug: str, *branches: str) -> None:
    store.redis.set(store.key(slug, "panic-fixers"), json.dumps(_fixer_links(*branches)))


def _read_pass(store: RedisStore, monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]) -> dict[str, Any]:
    class Ledger:
        def binned(self, slug: str) -> bool:
            return False

    monkeypatch.setattr(cli.ledger_client, "pass_state", lambda ledger, slug: document)
    wrapper: Any = object.__new__(cli._Wrapper)
    wrapper.state = {"store": store, "slug": SLUG, "ledger": Ledger()}
    wrapper.box = {}
    assert cli._Wrapper._read(wrapper) is True
    return wrapper.box


@pytest.mark.feature("panic-spares-fixer-branches")
def test_tick_pass_spares_every_swarms_recorded_fixer_branches_without_a_registration(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]]
) -> None:
    for slug, code in (("other", "d4e5f6"), ("third", "a7b8c9")):
        store.names.mint = lambda code=code: code
        store.create(SwarmConfig(slug, "/repo", max_eng=1, max_ci=1))
    _record(store, SLUG, f"ci-{CODE}-5149", f"ci-{CODE}-5150")
    _record(store, "other", f"engineer-{CODE}-10087")
    panic.engage(store, SLUG, "pressed", False, [], 0)
    branches = (MINE, f"ci-{CODE}-5149", f"ci-{CODE}-5150", f"engineer-{CODE}-10087", f"ci-{CODE}-0002")
    runs = Runs({repo: [_row(n, branch) for n, branch in enumerate(branches)] for repo in panic.REPOS})
    assert _tick(store, True, 1_000, runs) == [
        f"panic cancelled {repo} run {n} on {branches[n]}" for repo in panic.REPOS for n in (0, 4)
    ]


@pytest.mark.feature("panic-spares-fixer-branches")
def test_the_panic_button_spares_the_recorded_fixer_branches(
    store: RedisStore,
    counted: list[tuple[str, float, dict[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    _record(store, SLUG, f"ci-{CODE}-5149")
    runs = Runs({repo: [_row(7, MINE), _row(8, f"ci-{CODE}-5149")] for repo in panic.REPOS})
    monkeypatch.setattr(panic, "port", runs)
    args = cli.build_parser().parse_args([SLUG, "panic", "on", "--fix", FIX])
    args.handler(store, args)
    out = json.loads(capsys.readouterr().out)
    assert out["fixes"] == [FIX]
    assert out["cancelled"] == [f"{repo} run 7 on {MINE}" for repo in panic.REPOS]


@pytest.mark.feature("panic-spares-fixer-branches")
def test_the_pass_read_records_the_open_fixer_branches_of_its_document(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = {"tasks": _fixer_rows()}
    assert _read_pass(store, monkeypatch, document)["doc"] is document
    assert json.loads(store.redis.get(store.key(SLUG, "panic-fixers")) or "[]") == _fixer_links(
        "ci-323133-5149", "ci-323133-5150", "ci-323133-5150-2", "ci-323133-5153", "engineer-323133-10087"
    )


@pytest.mark.feature("panic-spares-fixer-branches")
def test_the_pass_read_drops_the_record_once_no_fixer_is_open(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record(store, SLUG, "ci-323133-5149")
    _read_pass(store, monkeypatch, {"tasks": _fixer_rows()[3:]})
    assert store.redis.exists(store.key(SLUG, "panic-fixers")) == 0


def test_fixer_links_keeps_the_fixers_listed_after_a_skipped_task() -> None:
    rows = [*_fixer_rows()[3:], *_fixer_rows()[:1]]
    assert panic.fixer_links(rows) == _fixer_links("ci-323133-5149")


@pytest.mark.feature("panic-spares-fixer-branches")
def test_the_fixer_task_pattern_names_the_ids_the_promotion_watch_and_the_crew_file() -> None:
    from scripts.swarm import promotion_watch, troubleshooting_crew

    crew = troubleshooting_crew.crew_id(1_794_870_000.0)
    fixers = [f"{promotion_watch.TASK_PREFIX}43992907244-1", *troubleshooting_crew.numbered(crew, "fix", 2)]
    others = [*troubleshooting_crew.numbered(crew, "scout", 1), f"{crew}-harden", "t-panic-spares-fix-prs"]
    malformed = ["doc-promo-1-0", "promo--0", "promo-1-", "promo-x-0", "crew-fix-1", "crew-1-fix-"]
    assert [panic.FIXER_TASK.fullmatch(task) is not None for task in fixers] == [True, True, True]
    assert [panic.FIXER_TASK.fullmatch(task) is not None for task in [*others, *malformed]] == [False] * 9


@pytest.mark.feature("panic-spares-fixer-branches")
def test_the_pass_read_writes_the_fixer_record_to_expire_after_fixers_ms(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []
    original = store.redis.set

    def recording(name: str, value: str, **options: Any) -> Any:
        sent.append(options)
        return original(name, value, **options)

    monkeypatch.setattr(store.redis, "set", recording)
    _read_pass(store, monkeypatch, {"tasks": _fixer_rows()[:1]})
    assert sent == [{"px": panic.FIXERS_MS}]


def test_the_panic_button_cancels_and_prints(
    store: RedisStore,
    counted: list[tuple[str, float, dict[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _branches(monkeypatch, {FIX: f"{MINE}-2"})
    runs = Runs({repo: [_row(7, MINE), _row(8, f"{MINE}-2")] for repo in panic.REPOS})
    monkeypatch.setattr(panic, "port", runs)
    args = cli.build_parser().parse_args([SLUG, "panic", "on", "--fix", FIX])
    before = cli.now_ms()
    args.handler(store, args)
    after = cli.now_ms()
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "panic": "on",
        "fixes": [FIX],
        "cancelled": [
            f"the-cloud-clockwork/agentihooks run 7 on {MINE}",
            f"the-cloud-clockwork/agentihooks-bundle run 7 on {MINE}",
            f"the-cloud-clockwork/agentihooks-bundle run 8 on {MINE}-2",
            f"the-cloud-clockwork/antoncore run 7 on {MINE}",
            f"the-cloud-clockwork/antoncore run 8 on {MINE}-2",
        ],
        "lab": NO_LAB,
    }
    current = panic.state(store, SLUG)
    assert current is not None
    assert current.reason == f"pressed on swarm {SLUG}"
    assert current.auto is False
    assert before <= current.since <= after


def test_the_panic_button_refuses_an_unknown_action():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([SLUG, "panic", "maybe"])


def test_the_panic_button_off_refuses_below_business_as_usual(store, counted):
    panic.engage(store, SLUG, "pressed", False, [], 0)
    now = cli.now_ms()
    _passes(store, 1, 1, now)
    _errors(store, 1, now)
    _errors(store, 1, now - WINDOW - 1)
    args = cli.build_parser().parse_args([SLUG, "panic", "off"])
    message = (
        r"^panic holds: business as usual is at 0\.50 with 1 errors in the last 15 minutes; "
        r"it clears at 0\.90 with fewer than 15$"
    )
    with pytest.raises(cli.SwarmError, match=message):
        args.handler(store, args)


def test_the_panic_button_off_clears_once_stable(store, counted, capsys):
    panic.engage(store, SLUG, "pressed", False, [], 0)
    args = cli.build_parser().parse_args([SLUG, "panic", "off"])
    args.handler(store, args)
    assert json.loads(capsys.readouterr().out) == {"panic": "off", "lab": ""}
    assert panic.state(store, SLUG) is None


def test_panic_status_reports_the_counter_and_level(store, counted, capsys):
    now = cli.now_ms()
    _errors(store, 2, now)
    args = cli.build_parser().parse_args([SLUG, "panic"])
    assert args.action == "status"
    args.handler(store, args)
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "panic": "off",
        "since": None,
        "reason": "",
        "fixes": [],
        "errors_last_hour": {"SpawnError": 2},
        "threshold": panic.THRESHOLD,
        "level": 1.0,
    }
    panic.engage(store, SLUG, "pressed", False, [FIX], 5)
    args.handler(store, args)
    out = json.loads(capsys.readouterr().out)
    assert (out["panic"], out["since"], out["reason"], out["fixes"]) == ("on", 5, "pressed", [FIX])


def test_panic_status_rounds_the_level_to_three_places(store, counted, capsys):
    _passes(store, 2, 1, cli.now_ms())
    args = cli.build_parser().parse_args([SLUG, "panic"])
    args.handler(store, args)
    assert json.loads(capsys.readouterr().out)["level"] == 0.667


def test_the_panic_button_without_a_fix_spares_only_the_merge_queue(store, counted, monkeypatch, capsys):
    runs = Runs({})
    monkeypatch.setattr(panic, "port", runs)
    args = cli.build_parser().parse_args([SLUG, "panic", "on"])
    assert args.fix == []
    args.handler(store, args)
    assert json.loads(capsys.readouterr().out) == {"panic": "on", "fixes": [], "cancelled": [], "lab": NO_LAB}


def _press(store: RedisStore, monkeypatch: pytest.MonkeyPatch, runs: Runs, *argv: str) -> None:
    monkeypatch.setattr(panic, "port", runs)
    args = cli.build_parser().parse_args([SLUG, *argv])
    args.handler(store, args)


def _busy() -> Runs:
    return Runs({repo: [_row(7, "engineer-a1b2c3-0001")] for repo in panic.REPOS})


@pytest.mark.feature("panic-fix-registration-checked")
def test_panic_on_refuses_a_seat_that_is_neither_the_master_nor_the_operator(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.store import AgentRecord

    asked = _branches(monkeypatch, {})
    runs = _busy()
    store.put_agent(SLUG, AgentRecord("engineer@a1b2c3-0001", "eng", "t1"))
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, runs, "--as", "engineer@a1b2c3-0001", "panic", "on", "--fix", BRANCH_FIX)
    assert str(caught.value) == "only the master or the operator presses panic on"
    assert panic.state(store, SLUG) is None
    assert runs.calls() == []
    assert asked == []


@pytest.mark.feature("panic-fix-registration-checked")
def test_a_seat_cannot_press_panic_on_as_the_master(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    assert cli.main([SLUG, "--as", "master@a1b2c3-0001", "panic", "on", "--fix", BRANCH_FIX]) == 1
    assert "cannot act as master@a1b2c3-0001" in capsys.readouterr().err
    assert panic.state(store, SLUG) is None


@pytest.mark.feature("panic-fix-registration-checked")
def test_the_swarm_master_presses_panic_on_with_a_checked_fix(
    store: RedisStore,
    counted: list[tuple[str, float, dict[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.swarm.store import AgentRecord

    asked = _branches(monkeypatch, {})
    store.put_agent(SLUG, AgentRecord("master@a1b2c3-0001", "master", "master"))
    _press(store, monkeypatch, _busy(), "--as", "master@a1b2c3-0001", "panic", "on", "--fix", BRANCH_FIX)
    cancelled = [f"{repo} run 7 on engineer-a1b2c3-0001" for repo in panic.REPOS]
    assert json.loads(capsys.readouterr().out) == {
        "panic": "on",
        "fixes": [BRANCH_FIX],
        "cancelled": cancelled,
        "lab": NO_LAB,
    }
    assert asked == []


@pytest.mark.feature("panic-drop-one-fix")
def test_the_master_drops_one_panic_fix_and_keeps_the_panic_and_its_other_fixes(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm.store import AgentRecord

    store.put_agent(SLUG, AgentRecord("master@a1b2c3-0001", "master", "master"))
    panic.engage(store, SLUG, "pressed", False, [FIX, BRANCH_FIX], 0)
    runs = _busy()
    _press(store, monkeypatch, runs, "--as", "master@a1b2c3-0001", "panic", "drop", "--fix", FIX)
    assert panic.state(store, SLUG) == panic.Panic(0, "pressed", False, (BRANCH_FIX,))
    assert json.loads(capsys.readouterr().out) == {"panic": "on", "fixes": [BRANCH_FIX], "lab": NO_LAB}
    assert runs.calls() == []


@pytest.mark.feature("panic-drop-one-fix")
def test_panic_drop_refuses_a_seat_that_is_neither_the_master_nor_the_operator(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.store import AgentRecord

    store.put_agent(SLUG, AgentRecord("engineer@a1b2c3-0001", "eng", "t1"))
    panic.engage(store, SLUG, "pressed", False, [FIX], 0)
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, _busy(), "--as", "engineer@a1b2c3-0001", "panic", "drop", "--fix", FIX)
    assert str(caught.value) == "only the master or the operator drops a panic fix"
    assert panic.state(store, SLUG) == panic.Panic(0, "pressed", False, (FIX,))


@pytest.mark.parametrize("named", [[], ["--fix", FIX, "--fix", BRANCH_FIX]])
@pytest.mark.feature("panic-drop-one-fix")
def test_panic_drop_takes_exactly_one_fix(store: RedisStore, monkeypatch: pytest.MonkeyPatch, named: list[str]) -> None:
    from scripts.swarm.store import AgentRecord

    store.put_agent(SLUG, AgentRecord("master@a1b2c3-0001", "master", "master"))
    panic.engage(store, SLUG, "pressed", False, [FIX, BRANCH_FIX], 0)
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, _busy(), "--as", "master@a1b2c3-0001", "panic", "drop", *named)
    assert str(caught.value) == "panic drop takes exactly one --fix"
    assert panic.state(store, SLUG) == panic.Panic(0, "pressed", False, (FIX, BRANCH_FIX))


@pytest.mark.feature("panic-drop-one-fix")
def test_panic_drop_refuses_a_fix_the_panic_does_not_hold(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm.store import AgentRecord

    store.put_agent(SLUG, AgentRecord("master@a1b2c3-0001", "master", "master"))
    panic.engage(store, SLUG, "pressed", False, [BRANCH_FIX], 0)
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, _busy(), "--as", "master@a1b2c3-0001", "panic", "drop", "--fix", FIX)
    assert str(caught.value) == f"the panic on swarm {SLUG} holds no fix {FIX}"
    assert panic.state(store, SLUG) == panic.Panic(0, "pressed", False, (BRANCH_FIX,))


@pytest.mark.feature("panic-drop-one-fix")
def test_panic_drop_refuses_while_no_panic_holds_the_swarm(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm.store import AgentRecord

    store.put_agent(SLUG, AgentRecord("master@a1b2c3-0001", "master", "master"))
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, _busy(), "--as", "master@a1b2c3-0001", "panic", "drop", "--fix", FIX)
    assert str(caught.value) == f"no panic holds swarm {SLUG}"
    assert panic.state(store, SLUG) is None


SHAPE = (
    "{fix} is not a fix link: name a pull request https://github.com/OWNER/NAME/pull/NUMBER or a branch "
    "https://github.com/OWNER/NAME/tree/BRANCH"
)
OUTSIDE = (
    "{fix} is outside the panic repositories the-cloud-clockwork/agentihooks, "
    "the-cloud-clockwork/agentihooks-bundle, the-cloud-clockwork/antoncore"
)


@pytest.mark.parametrize(
    ("fix", "refusal"),
    [
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/dev", "{fix} names dev, which panic never spares"),
        ("https://github.com/the-cloud-clockwork/antoncore/tree/main", "{fix} names main, which panic never spares"),
        ("https://github.com/someone/else/tree/fix-branch", OUTSIDE),
        ("https://github.com/someone/else/pull/1", OUTSIDE),
        ("https://github.com/The-Cloud-Clockwork/agentihooks/pull/1", OUTSIDE),
        ("https://github.com/the-cloud-clockwork/agentihooks/pull/one", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/pull/1/files", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/issues/1", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/", SHAPE),
        ("https://github.com/the-cloud-clockwork/tree/fix-branch", SHAPE),
        ("http://github.com/the-cloud-clockwork/agentihooks/pull/1", SHAPE),
        ("the-cloud-clockwork/agentihooks/pull/1", SHAPE),
        (f"{FIX}\n", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/dev?x=1", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/dev/", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/refs/heads/dev", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/a..b", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/fix branch", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree/.hidden", SHAPE),
        ("https://github.com/the-cloud-clockwork/agentihooks/tree//dev", SHAPE),
    ],
)
@pytest.mark.feature("panic-fix-registration-checked")
def test_panic_on_refuses_a_fix_link_before_it_stores_or_cancels_anything(
    store: RedisStore,
    counted: list[tuple[str, float, dict[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    fix: str,
    refusal: str,
) -> None:
    asked = _branches(monkeypatch, {})
    panic.engage(store, SLUG, "pressed", False, [BRANCH_FIX], 0)
    runs = _busy()
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, runs, "panic", "on", "--fix", BRANCH_FIX, "--fix", fix)
    assert str(caught.value) == refusal.format(fix=fix)
    assert panic.state(store, SLUG) == panic.Panic(0, "pressed", False, (BRANCH_FIX,))
    assert runs.calls() == []
    assert asked == []


@pytest.mark.parametrize("head", ["dev", "main"])
@pytest.mark.feature("panic-fix-registration-checked")
def test_panic_on_refuses_a_fix_pull_request_whose_head_is_dev_or_main(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch, head: str
) -> None:
    asked = _branches(monkeypatch, {FIX: head})
    runs = _busy()
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, runs, "panic", "on", "--fix", FIX)
    assert str(caught.value) == f"{FIX} names {head}, which panic never spares"
    assert panic.state(store, SLUG) is None
    assert runs.calls() == []
    assert asked == [FIX]


@pytest.mark.feature("panic-fix-registration-checked")
def test_panic_on_refuses_a_fix_pull_request_it_cannot_read(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import stack
    from scripts.swarm.store import SwarmError

    def unreadable(url: str, refused: str) -> None:
        raise SwarmError(f"{refused}: boom")

    monkeypatch.setattr(stack, "change", unreadable)
    runs = _busy()
    with pytest.raises(cli.SwarmError) as caught:
        _press(store, monkeypatch, runs, "panic", "on", "--fix", FIX)
    assert str(caught.value) == f"cannot read pull request {FIX}: boom"
    assert panic.state(store, SLUG) is None
    assert runs.calls() == []


@pytest.mark.parametrize(
    "fix",
    [
        BRANCH_FIX,
        "https://github.com/the-cloud-clockwork/agentihooks-bundle/tree/devops",
        "https://github.com/the-cloud-clockwork/antoncore/tree/fix/main",
        "https://github.com/the-cloud-clockwork/agentihooks/tree/diffcheck/ci-1_2.v3",
    ],
)
@pytest.mark.feature("panic-fix-registration-checked")
def test_a_branch_link_of_a_panic_repository_passes_the_check_unread(monkeypatch: pytest.MonkeyPatch, fix: str) -> None:
    asked = _branches(monkeypatch, {})
    assert panic.check_fix(fix) is None
    assert asked == []


@pytest.mark.feature("panic-fix-registration-checked")
def test_a_fix_pull_request_passes_the_check_after_one_read_of_its_head(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _branches(monkeypatch, {FIX: "fix-branch"})
    assert panic.check_fix(FIX) is None
    assert asked == [FIX]


def test_panic_found_names_the_panic_since_its_start(store):
    from scripts.swarm.blockers import Blocker

    panic.engage(store, SLUG, "reason", True, [], 3)
    assert blocker_window._panic_found(store, SLUG, 5) == {"panic": Blocker("panic", 3, 5, 0, "reason")}


def test_the_panic_notice_is_rate_limited_per_swarm(store, master):
    panic.engage(store, SLUG, "reason", True, [], blocker_window._now_ms(store))
    blocker_window.pretool("Read", {}, SID)
    assert store.redis.get(store.key(SLUG, "blockers-notice")) is not None


@pytest.fixture
def master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    master_coding.record_seat(SID, SLUG, "master@sw")
    return tmp_path


@pytest.mark.feature("panic-master-keeps-every-tool")
def test_panic_opens_the_master_hotfix_window(store, master):
    panic.engage(store, SLUG, "60 errors in the last hour", True, [], blocker_window._now_ms(store))
    notice, block = blocker_window.pretool("Write", {"file_path": "/etc/x"}, SID)
    assert master_coding.is_open(SID)
    assert notice is not None
    assert "panic for 0 minutes" in notice
    assert f"agentihooks swarm {SLUG} panic on --fix <pr url>" in notice
    assert block is None


def test_the_master_may_press_the_panic_button(store, master):
    panic.engage(store, SLUG, "pressed", False, [], blocker_window._now_ms(store))
    command = f"agentihooks swarm {SLUG} panic on --fix {FIX}"
    assert blocker_window.pretool("Bash", {"command": command}, SID)[1] is None


@pytest.mark.parametrize(
    "command",
    [
        "git push origin x",
        "/usr/bin/git push",
        "gh run rerun 5",
        "gh workflow run t.yml",
        "kubectl apply -f a.yaml",
        "kubectl rollout restart deploy/x",
        "helm upgrade x y",
        "helm install x y",
        "argocd app sync x",
    ],
)
@pytest.mark.feature("panic-master-keeps-every-tool")
def test_panic_freezes_pushes_reruns_dispatches_and_deploys_for_other_sessions(bound: None, store, command):
    panic.engage(store, SLUG, "60 errors in the last hour", True, [], 0)
    notice, block = blocker_window.pretool("Bash", {"command": command}, "engineer-session")
    assert notice is None
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="60 errors in the last hour")


def _checkout(root: Path, branch: str, origin: str = "git@github.com:The-Cloud-Clockwork/agentihooks.git") -> str:
    subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    return str(root)


@pytest.mark.parametrize(
    "command", ["git push origin fix-branch", "gh run rerun 5", "gh workflow run t.yml --ref fix-branch"]
)
@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_spares_a_push_from_the_head_branch_of_a_registered_fix(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    asked = _branches(monkeypatch, {FIX: "fix-branch"})
    _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "60 errors in the last hour", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd) == (None, None)
    assert asked == [FIX]


@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_still_refuses_a_push_from_any_other_branch(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "60 errors in the last hour", False, [FIX], 0)
    cwd = _checkout(tmp_path / "other", "other-branch")
    block = blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="60 errors in the last hour")


@pytest.mark.parametrize(
    ("branch", "command", "spared"),
    [
        ("fix-branch", "git push lab HEAD:refs/grade/t1", True),
        ("other-branch", "git push -o branch=fix-branch lab HEAD:refs/grade/t1", True),
        ("fix-branch", "git push -o branch=other lab HEAD:refs/grade/t1", False),
        ("other-branch", "git push lab HEAD:refs/grade/t1", False),
        ("other-branch", "git push -o branch=fix-branch origin HEAD:fix-branch", True),
        ("other-branch", "git push --push-option branch=x origin HEAD:fix-branch", True),
        ("other-branch", "git push origin HEAD:fix-branch -o", True),
    ],
)
@pytest.mark.feature("lab-remote-in-workspaces")
def test_panic_spares_a_lab_push_only_when_it_grades_or_relays_a_registered_fix(
    bound: None,
    store: RedisStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    branch: str,
    command: str,
    spared: bool,
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "60 errors in the last hour", False, [FIX], 0)
    cwd = _checkout(tmp_path / "work", branch)
    subprocess.run(["git", "-C", cwd, "remote", "add", "lab", "http://lab.invalid/agentihooks.git"], check=True)
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1]
    frozen = blocker_window.FROZEN.format(slug=SLUG, reason="60 errors in the last hour")
    assert block == (None if spared else frozen)


@pytest.mark.feature("panic-fix-branch-pushes")
def test_the_hook_payload_cwd_decides_the_fix_branch_push(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "60 errors in the last hour", False, [FIX], 0)
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "session_id": "engineer-session"}
    fix = _checkout(tmp_path / "fix", "fix-branch")
    other = _checkout(tmp_path / "other", "other-branch")
    assert blocker_window.checked({**payload, "cwd": fix}) is None
    assert blocker_window.checked({**payload, "cwd": other}) is not None
    assert blocker_window.checked(payload) is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_push_after_cd_into_the_fix_worktree_is_spared(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    primary = _checkout(tmp_path / "primary", "dev")
    fix = _checkout(tmp_path / "fix", "fix-branch")
    other = _checkout(tmp_path / "other", "other-branch")
    spared = blocker_window.pretool("Bash", {"command": f"cd {fix} && git push -u origin HEAD"}, "s", primary)
    refused = blocker_window.pretool("Bash", {"command": f"cd {other} && git push"}, "s", fix)
    assert spared == (None, None)
    assert refused[1] is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_push_without_a_session_directory_stays_frozen(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    fix = _checkout(tmp_path / "fix", "fix-branch")
    refused = blocker_window.pretool("Bash", {"command": f"cd {fix} && git push"}, "s", "")
    assert refused[1] is not None
    assert blocker_window._directory("", [str(fix)]) == ""


@pytest.mark.feature("panic-publish-holds")
def test_the_push_check_passes_any_branch_without_panic(store: RedisStore, tmp_path: Path) -> None:
    cwd = _checkout(tmp_path / "other", "other-branch")
    assert blocker_window.held_push(cwd, "other-branch", SLUG) is None


@pytest.mark.parametrize("fix", [FIX, BRANCH_FIX])
@pytest.mark.feature("panic-publish-holds")
def test_the_push_check_spares_a_registered_fix_from_a_checkout_on_another_branch(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fix: str
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [fix], 0)
    cwd = _checkout(tmp_path / "work", "engineer-branch")
    assert blocker_window.held_push(cwd, "fix-branch", SLUG) is None


@pytest.mark.feature("panic-publish-holds")
def test_the_push_check_refuses_a_branch_no_fix_names(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX, BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.held_push(cwd, "other-branch", SLUG) == blocker_window.FROZEN.format(
        slug=SLUG, reason="reason"
    )


@pytest.mark.feature("panic-publish-holds")
def test_the_push_check_refuses_a_fix_whose_config_routes_the_push(store: RedisStore, tmp_path: Path) -> None:
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    subprocess.run(["git", "-C", cwd, "config", "remote.origin.pushurl", "git@github.com:else/where.git"], check=True)
    assert blocker_window.held_push(cwd, "fix-branch", SLUG) == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.feature("panic-publish-holds")
def test_the_push_check_passes_when_the_store_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm import store
    from scripts.swarm.store import Unreachable

    def connect() -> RedisStore:
        raise Unreachable("Redis is unreachable")

    monkeypatch.setattr(store, "connect", connect)
    cwd = _checkout(tmp_path / "other", "other-branch")
    assert blocker_window.held_push(cwd, "other-branch", SLUG) is None


@pytest.mark.parametrize("command", ["kubectl apply -f a.yaml", "git push && helm upgrade x y"])
def test_a_fix_branch_session_still_waits_to_deploy(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1] is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_same_named_branch_in_another_repository_stays_frozen(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked = _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch", "https://github.com/the-cloud-clockwork/antoncore.git")
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", cwd)[1] is not None
    assert asked == []


@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_refuses_a_push_when_the_fix_cannot_be_read(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm import stack
    from scripts.swarm.store import SwarmError

    def change(url: str, refused: str) -> None:
        raise SwarmError(refused)

    monkeypatch.setattr(stack, "change", change)
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", cwd)[1] is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_refuses_a_push_outside_a_git_checkout(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", str(tmp_path))[1] is not None


@pytest.mark.parametrize(
    "command", ["git push -u origin HEAD", "gh run rerun 5", "gh workflow run t.yml --ref fix-branch"]
)
@pytest.mark.feature("panic-fix-branch-first-push")
def test_panic_spares_the_first_push_of_a_registered_fix_branch(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    asked = _branches(monkeypatch, {})
    _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "60 errors in the last hour", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd) == (None, None)
    assert asked == []


@pytest.mark.parametrize(
    ("branch", "origin"),
    [
        ("other-branch", "git@github.com:The-Cloud-Clockwork/agentihooks.git"),
        ("fix-branch", "https://github.com/the-cloud-clockwork/antoncore.git"),
    ],
)
@pytest.mark.feature("panic-fix-branch-first-push")
def test_panic_refuses_a_push_from_a_branch_no_fix_registers(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, branch: str, origin: str
) -> None:
    _branches(monkeypatch, {})
    panic.engage(store, SLUG, "60 errors in the last hour", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "work", branch, origin)
    block = blocker_window.pretool("Bash", {"command": "git push -u origin HEAD"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="60 errors in the last hour")


@pytest.mark.parametrize(
    "command",
    [
        "git push origin fix-branch:dev",
        "git push origin HEAD:main",
        "git push origin HEAD:refs/heads/dev",
        "git push origin dev",
        "git push origin fix-branch dev",
        "git push origin :",
        "git push origin dev:HEAD",
        "git push upstream fix-branch",
        "git push origin HEAD:dev>/dev/null",
        "git push 2> err.txt origin HEAD:dev",
        "git push --all origin",
        "git push --branches origin",
        "git push --mirror origin",
        "git push origin --tags",
        "git push --follow-tags origin fix-branch",
        "git push --prune origin fix-branch",
        "git push --repo=elsewhere",
        "git push --repo elsewhere fix-branch",
        "gh workflow run t.yml",
        "gh workflow run t.yml --ref",
        "gh workflow run t.yml --ref dev",
        "gh workflow run t.yml --ref=main",
        "gh workflow run t.yml -r dev",
        "gh workflow run t.yml -rmain",
        "gh workflow run t.yml --ref fix-branch -r dev",
        "gh workflow run t.yml --ref fix-branch -R the-cloud-clockwork/antoncore",
        "gh workflow run t.yml --ref fix-branch --repo=the-cloud-clockwork/antoncore",
        "gh run rerun 5 -R the-cloud-clockwork/antoncore",
        "gh run rerun 5 --repo the-cloud-clockwork/antoncore",
        "gh -R the-cloud-clockwork/antoncore workflow run t.yml --ref fix-branch",
        "gh --repo=the-cloud-clockwork/antoncore run rerun 5",
        "GIT_DIR=/elsewhere/.git git push",
        "GIT_WORK_TREE=/elsewhere git push origin fix-branch",
        "export GIT_DIR=/elsewhere/.git; git push origin HEAD",
        "env GIT_COMMON_DIR=/elsewhere git push origin fix-branch",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=push.default GIT_CONFIG_VALUE_0=upstream git push",
        "git -c push.default=upstream push",
        "git --git-dir=.git push origin fix-branch",
        "git --no-pager -C . push origin dev",
    ],
)
@pytest.mark.feature("panic-fix-spare-reads-target")
def test_a_fix_branch_session_stays_frozen_for_a_command_that_writes_another_ref(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    _branches(monkeypatch, {})
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin",
        "git push -u origin HEAD 2>&1 | tail -3",
        "git push origin fix-branch",
        "git push origin +fix-branch",
        "git push --force-with-lease origin HEAD:fix-branch",
        "git push origin fix-branch:refs/heads/fix-branch >/dev/null 2>&1",
        "git push origin fix-branch < /dev/null",
        "git -C . push origin fix-branch",
        "gh workflow run t.yml --ref fix-branch",
        "gh workflow run t.yml --ref=refs/heads/fix-branch",
        "gh workflow run t.yml -r fix-branch",
        "gh workflow run t.yml -r=fix-branch",
        "gh workflow run t.yml -rfix-branch",
        "gh workflow run t.yml -r dev --ref fix-branch",
        "gh run rerun 5 --failed",
    ],
)
@pytest.mark.feature("panic-fix-spare-reads-target")
def test_a_fix_branch_session_keeps_a_command_that_writes_only_its_branch(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    _branches(monkeypatch, {})
    _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd) == (None, None)


def _successor(root: Path, remote: str = "origin") -> str:
    cwd = _checkout(root, "engineer-successor")
    subprocess.run(["git", "-C", cwd, "config", "branch.engineer-successor.remote", remote], check=True)
    subprocess.run(["git", "-C", cwd, "config", "branch.engineer-successor.merge", "refs/heads/fix-branch"], check=True)
    return cwd


@pytest.mark.parametrize("fix", [FIX, BRANCH_FIX])
@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:fix-branch",
        "git push -u origin HEAD:fix-branch",
        "git push origin engineer-successor:refs/heads/fix-branch",
        "gh workflow run t.yml --ref fix-branch",
        "gh run rerun 5",
    ],
)
@pytest.mark.feature("panic-spares-a-successor-of-a-fix")
def test_a_successor_branch_tracking_a_registered_fix_keeps_its_fix_pushes_dispatches_and_reruns(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fix: str, command: str
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "reason", False, [fix], 0)
    cwd = _successor(tmp_path / "successor")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd) == (None, None)


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin HEAD",
        "git push -u origin HEAD:dev",
        "git push origin HEAD:fix-branch HEAD:dev",
        "git push origin engineer-successor",
        "gh workflow run t.yml --ref dev",
        "gh workflow run t.yml --ref engineer-successor",
    ],
)
@pytest.mark.feature("panic-spares-a-successor-of-a-fix")
def test_a_successor_branch_tracking_a_registered_fix_stays_frozen_for_any_other_target(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    cwd = _successor(tmp_path / "successor")
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.feature("panic-spares-a-successor-of-a-fix")
def test_a_rerun_from_a_branch_tracking_a_fix_on_another_remote_stays_frozen(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    cwd = _successor(tmp_path / "successor", "elsewhere")
    block = blocker_window.pretool("Bash", {"command": "gh run rerun 5"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.parametrize("fix", [FIX, BRANCH_FIX])
@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_fix_branch_session_rerunning_a_dev_run_stays_frozen(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fix: str
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    read = _heads(monkeypatch, "dev")
    panic.engage(store, SLUG, "reason", False, [fix], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    block = blocker_window.pretool("Bash", {"command": "gh run rerun 7"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")
    assert read == ["repos/the-cloud-clockwork/agentihooks/actions/runs/7"]


@pytest.mark.parametrize(
    ("command", "resource"),
    [
        ("gh run rerun 7", "runs/7"),
        ("gh run rerun 7 --failed", "runs/7"),
        ("gh run rerun -d --failed 7", "runs/7"),
        ("gh run rerun --debug 7", "runs/7"),
        ("gh run rerun --job 9", "jobs/9"),
        ("gh run rerun --job=9 -d", "jobs/9"),
        ("gh run rerun -j 9", "jobs/9"),
        ("gh run rerun -j9", "jobs/9"),
        ("gh run rerun -j=9", "jobs/9"),
    ],
)
@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_fix_branch_session_keeps_a_rerun_of_one_run_or_job_on_its_branch(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, resource: str
) -> None:
    _branches(monkeypatch, {})
    read = _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd) == (None, None)
    assert read == [f"repos/the-cloud-clockwork/agentihooks/actions/{resource}"]


@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_rerun_reads_its_run_from_the_repository_origin_names(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {})
    read = _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "reason", False, ["https://github.com/the-cloud-clockwork/antoncore/tree/fix-branch"], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch", "https://github.com/the-cloud-clockwork/antoncore.git")
    assert blocker_window.pretool("Bash", {"command": "gh run rerun 7"}, "engineer-session", cwd) == (None, None)
    assert read == ["repos/the-cloud-clockwork/antoncore/actions/runs/7"]


@pytest.mark.parametrize(
    "command",
    [
        "gh run rerun",
        "gh run rerun --failed",
        "gh run rerun abc",
        "gh run rerun 7 8",
        "gh run rerun 7 --job 9",
        "gh run rerun --yes 7",
        "gh run rerun --job",
        "gh run rerun 7 --job",
        "gh run rerun 7 -j",
        "gh run rerun -j x",
    ],
)
@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_rerun_naming_no_single_run_or_job_stays_frozen_and_reads_no_run(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    _branches(monkeypatch, {})
    read = _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")
    assert read == []


@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_fix_branch_session_rerunning_another_fix_branch_run_stays_frozen(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {})
    read = _heads(monkeypatch, "other-fix")
    other = "https://github.com/the-cloud-clockwork/agentihooks/tree/other-fix"
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX, other], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    block = blocker_window.pretool("Bash", {"command": "gh run rerun 7"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")
    assert read == ["repos/the-cloud-clockwork/agentihooks/actions/runs/7"]


@pytest.mark.parametrize(("head", "spared"), [("fix-branch", True), ("engineer-successor", False)])
@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_successor_rerun_is_spared_only_for_a_run_on_the_fix_branch_it_tracks(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, head: str, spared: bool
) -> None:
    _branches(monkeypatch, {})
    read = _heads(monkeypatch, head)
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _successor(tmp_path / "successor")
    block = blocker_window.pretool("Bash", {"command": "gh run rerun 7"}, "engineer-session", cwd)[1]
    assert block == (None if spared else blocker_window.FROZEN.format(slug=SLUG, reason="reason"))
    assert read == ["repos/the-cloud-clockwork/agentihooks/actions/runs/7"]


@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_rerun_from_a_branch_no_fix_registers_reads_no_run(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {})
    read = _heads(monkeypatch, "fix-branch")
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "other", "other-branch")
    block = blocker_window.pretool("Bash", {"command": "gh run rerun 7"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")
    assert read == []


@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_rerun_of_a_run_that_cannot_be_read_stays_frozen(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {})
    monkeypatch.setattr(panic, "port", lambda repository: Unread())
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    block = blocker_window.pretool("Bash", {"command": "gh run rerun 7"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.parametrize(("body", "head"), [({"head_branch": "fix-branch"}, "fix-branch"), ({"head_branch": None}, "")])
@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_run_read_returns_its_head_branch_through_its_repository_port(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, str | None], head: str
) -> None:
    ported: list[str] = []
    port = FakeSourceControl({"download": json.dumps(body).encode()})

    def made(repository: str) -> FakeSourceControl:
        ported.append(repository)
        return port

    monkeypatch.setattr(panic, "port", made)
    assert panic.run_branch("the-cloud-clockwork/antoncore", "jobs/9") == head
    assert ported == ["the-cloud-clockwork/antoncore"]
    assert [(call.operation, call.args) for call in port.calls] == [
        ("download", ("repos/the-cloud-clockwork/antoncore/actions/jobs/9",))
    ]
    assert panic.RUN_READ_SECONDS - 5 < port.calls[0].deadline.remaining <= panic.RUN_READ_SECONDS == 10.0


@pytest.mark.parametrize(
    ("port", "why"), [(Unread(), "HTTP 404"), (FakeSourceControl({"download": b"{}"}), "'head_branch'")]
)
@pytest.mark.feature("panic-rerun-reads-run-branch")
def test_a_run_read_that_fails_refuses_with_its_path_and_reason(
    monkeypatch: pytest.MonkeyPatch, port: FakeSourceControl, why: str
) -> None:
    monkeypatch.setattr(panic, "port", lambda repository: port)
    with pytest.raises(cli.SwarmError) as caught:
        panic.run_branch("the-cloud-clockwork/agentihooks", "runs/7")
    assert str(caught.value) == f"cannot read repos/the-cloud-clockwork/agentihooks/actions/runs/7: {why}"


@pytest.mark.parametrize(("remote", "tracked"), [("origin", "fix-branch"), ("elsewhere", "")])
@pytest.mark.feature("panic-spares-a-successor-of-a-fix")
def test_a_branch_tracks_only_a_branch_on_origin(tmp_path: Path, remote: str, tracked: str) -> None:
    cwd = _successor(tmp_path / "successor", remote)
    assert blocker_window._tracked(cwd, "engineer-successor") == tracked
    assert blocker_window._tracked(_checkout(tmp_path / "plain", "plain"), "plain") == ""


@pytest.mark.parametrize(("cwd", "fixes"), [("", (FIX,)), ("/work", ())])
@pytest.mark.feature("panic-spares-a-successor-of-a-fix")
def test_a_rerun_without_a_directory_or_a_fix_reads_no_git(
    monkeypatch: pytest.MonkeyPatch, cwd: str, fixes: tuple[str, ...]
) -> None:
    asked = _reads(monkeypatch, {})
    assert blocker_window._works_on_fix(cwd, fixes, ["gh", "run", "rerun", "5"]) is False
    assert asked == []


@pytest.mark.feature("panic-fix-spare-reads-target")
def test_git_c_decides_the_checkout_a_push_is_spared_in(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {})
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    fix = _checkout(tmp_path / "fix", "fix-branch")
    other = _checkout(tmp_path / "other", "other-branch", "git@github.com:The-Cloud-Clockwork/antoncore.git")
    frozen = blocker_window.FROZEN.format(slug=SLUG, reason="reason")
    assert blocker_window.pretool("Bash", {"command": f"git -C {other} push"}, "s", fix)[1] == frozen
    assert blocker_window.pretool("Bash", {"command": f"git -C {other} push origin fix-branch"}, "s", fix)[1] == frozen
    assert blocker_window.pretool("Bash", {"command": f"git -C {fix} push origin fix-branch"}, "s", other) == (
        None,
        None,
    )
    assert blocker_window.pretool("Bash", {"command": "git -C ../fix push"}, "s", other) == (None, None)
    assert blocker_window.pretool("Bash", {"command": f"git -C {fix} push"}, "s", "")[1] == frozen


@pytest.mark.parametrize(
    ("key", "value", "command", "frozen"),
    [
        ("push.default", "upstream", "git push", True),
        ("push.default", "matching", "git push origin", True),
        ("remote.origin.push", "+refs/heads/*:refs/heads/*", "git push origin fix-branch", True),
        ("remote.origin.pushurl", "https://github.com/someone/else.git", "git push origin fix-branch", True),
        ("remote.origin.mirror", "true", "git push origin fix-branch", True),
        (
            "url.git@github.com:someone/.pushInsteadOf",
            "git@github.com:The-Cloud-Clockwork/",
            "git push origin fix-branch",
            True,
        ),
        ("branch.fix-branch.remote", "upstream", "git push", True),
        ("branch.fix-branch.pushRemote", "elsewhere", "git push", True),
        ("remote.pushDefault", "elsewhere", "git push", True),
        ("push.default", "current", "git push", False),
        ("push.default", "simple", "git push", False),
        ("branch.fix-branch.remote", "origin", "git push", False),
        ("remote.pushDefault", "origin", "git push origin HEAD", False),
        ("push.followTags", "true", "git push origin fix-branch", True),
        ("push.followTags", "false", "git push origin fix-branch", False),
    ],
)
@pytest.mark.feature("panic-fix-spare-reads-target")
def test_a_push_git_config_may_route_elsewhere_stays_frozen(
    store: RedisStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    key: str,
    value: str,
    command: str,
    frozen: bool,
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    _branches(monkeypatch, {})
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    subprocess.run(["git", "-C", cwd, "config", key, value], check=True)
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1]
    assert block == (blocker_window.FROZEN.format(slug=SLUG, reason="reason") if frozen else None)


@pytest.mark.feature("panic-fix-spare-reads-target")
def test_a_git_config_that_cannot_be_read_counts_as_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, tuple[str, ...]]] = []

    def git(cwd: str, *args: str) -> str:
        asked.append((cwd, args))
        return ""

    monkeypatch.setattr(blocker_window, "_git", git)
    assert blocker_window._routed("/work") is True
    assert asked == [("/work", ("config", "--list"))]


@pytest.mark.parametrize(("added", "frozen"), [((), False), (("https://github.com/someone/else.git",), True)])
@pytest.mark.feature("panic-fix-spare-one-push-url")
def test_a_fix_push_from_a_checkout_whose_origin_lists_two_urls_stays_frozen(
    store: RedisStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    added: tuple[str, ...],
    frozen: bool,
) -> None:
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    _branches(monkeypatch, {})
    panic.engage(store, SLUG, "reason", False, [BRANCH_FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    for url in added:
        subprocess.run(["git", "-C", cwd, "remote", "set-url", "--add", "origin", url], check=True)
    expected = blocker_window.FROZEN.format(slug=SLUG, reason="reason") if frozen else None
    command = {"command": "git push origin fix-branch"}
    assert blocker_window.pretool("Bash", command, "engineer-session", cwd)[1] == expected
    assert blocker_window.held_push(cwd, "fix-branch", SLUG) == expected


@pytest.mark.parametrize(
    "command",
    [
        "git -C /work push",
        "/usr/bin/git -C /work push origin x",
        "git --no-pager push origin x",
        "git -c a=b push",
        "git >/dev/null push origin x",
        ">/dev/null git push origin x",
        "gh -R o/r workflow run ci.yml --ref dev",
        "gh --repo o/r run rerun 5",
        "gh --repo=o/r workflow run ci.yml",
    ],
)
@pytest.mark.feature("panic-fix-spare-reads-target")
def test_panic_freezes_a_push_behind_git_global_options_or_a_redirection(
    bound: None, store: RedisStore, command: str
) -> None:
    panic.engage(store, SLUG, "reason", True, [], 0)
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session")[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.parametrize(
    "command",
    [
        "kubectl -n ns apply -f x.yaml",
        "kubectl --namespace=ns apply -f x.yaml",
        "kubectl --context c rollout restart deploy/x",
        "kubectl --insecure-skip-tls-verify --kubeconfig k -v 6 apply -f x.yaml",
        "/usr/local/bin/kubectl -s https://k:6443 --token t apply -f x.yaml",
        "helm --kube-context c upgrade r ch",
        "helm -n ns install r ch",
        "helm --debug --kubeconfig k upgrade --install r ch",
        "argocd --grpc-web app sync a",
        "argocd --server s --auth-token t app sync a",
        "argocd app --grpc-web sync a",
        "argocd -H 'x: y' app --server s sync a",
        "gh workflow --repo o/r run ci.yml --ref dev",
        "gh run -R o/r rerun 5",
    ],
)
@pytest.mark.feature("panic-freeze-global-options")
def test_panic_freezes_a_deploy_behind_kubectl_helm_argocd_or_gh_global_options(
    bound: None, store: RedisStore, command: str
) -> None:
    panic.engage(store, SLUG, "reason", True, [], 0)
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session")[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.parametrize(
    "command",
    [
        "gh api -X POST repos/o/r/actions/workflows/ci.yml/dispatches -f ref=dev",
        "gh api repos/{owner}/{repo}/actions/workflows/ci.yml/dispatches -f ref=dev",
        "gh api --method POST /repos/o/r/dispatches -f event_type=go",
        "gh api -H 'Accept: application/vnd.github+json' https://api.github.com/repos/o/r/dispatches/",
        "gh api --input body.json repos/o/r/actions/workflows/7/dispatches",
        "gh api repos/o/r/dispatches?per_page=1",
        "gh api --paginate repos/o/r/actions/workflows/ci.yml/dispatches",
    ],
)
@pytest.mark.feature("panic-freeze-global-options")
def test_panic_freezes_a_workflow_dispatch_through_gh_api(bound: None, store: RedisStore, command: str) -> None:
    panic.engage(store, SLUG, "reason", True, [], 0)
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session")[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.feature("panic-freeze-global-options")
def test_a_fix_branch_session_stays_frozen_for_a_gh_api_dispatch(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    command = "gh api repos/{owner}/{repo}/actions/workflows/t.yml/dispatches -f ref=fix-branch"
    block = blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(slug=SLUG, reason="reason")


@pytest.mark.parametrize(
    "command",
    [
        "kubectl -n apply get pods",
        "kubectl --context rollout get deploy",
        "helm -n upgrade list",
        "argocd app --server sync get a",
        "argocd --grpc-web app get a",
        "gh api repos/o/r/actions/workflows",
        "gh api -f q=dispatches graphql",
        "gh api repos/o/r/actions/runs -q .dispatches",
        "gh api -X POST --paginate",
        "gh api repos/o/r/dispatchesX",
        "gh api --paginate -X",
    ],
)
@pytest.mark.feature("panic-freeze-global-options")
def test_a_read_behind_global_options_stays_unfrozen(bound: None, store: RedisStore, command: str) -> None:
    panic.engage(store, SLUG, "reason", True, [], 0)
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session") == (None, None)


@pytest.mark.feature("panic-freeze-global-options")
def test_every_frozen_program_has_a_global_option_set() -> None:
    programs = {prefix[0] for prefix in blocker_window._FROZEN_COMMANDS}
    assert programs <= set(blocker_window._GLOBAL_VALUES)


_KUBECTL_VALUE_OPTIONS = (
    "-n --namespace -s --server -v --v --as --as-group --as-uid --as-user-extra --cache-dir --certificate-authority "
    "--client-certificate --client-key --cluster --context --kubeconfig --kuberc --log-flush-frequency --password "
    "--profile --profile-output --proxy-url --request-timeout --tls-server-name --token --user --username --vmodule"
)
_HELM_VALUE_OPTIONS = (
    "-n --namespace --burst-limit --color --colour --content-cache --kube-apiserver --kube-as-group --kube-as-user "
    "--kube-ca-file --kube-context --kube-tls-server-name --kube-token --kubeconfig --qps --registry-config "
    "--repository-cache --repository-config"
)
_ARGOCD_VALUE_OPTIONS = (
    "-H --header --argocd-context --auth-token --client-crt --client-crt-key --config --controller-name "
    "--grpc-web-root-path --http-retry-max --kube-context --logformat --loglevel --port-forward-namespace "
    "--redis-compress --redis-haproxy-name --redis-name --repo-server-name --server --server-crt --server-name"
)


@pytest.mark.parametrize(
    ("command", "option"),
    [
        *(("kubectl apply", option) for option in _KUBECTL_VALUE_OPTIONS.split()),
        *(("helm upgrade", option) for option in _HELM_VALUE_OPTIONS.split()),
        *(("argocd app sync", option) for option in _ARGOCD_VALUE_OPTIONS.split()),
    ],
)
@pytest.mark.feature("panic-freeze-global-options")
def test_a_global_option_takes_its_value_before_a_frozen_subcommand(command: str, option: str) -> None:
    program, *subcommand = command.split()
    assert blocker_window._freezes([program, option, "value", *subcommand]) is True


@pytest.mark.parametrize(
    "option",
    "-F --field -f --raw-field -H --header -X --method -q --jq -t --template -p --preview --hostname "
    "--input --cache".split(),
)
@pytest.mark.feature("panic-freeze-global-options")
def test_a_gh_api_option_takes_its_value_before_the_endpoint(option: str) -> None:
    assert blocker_window._freezes(["gh", "api", option, "value", "repos/o/r/dispatches"]) is True


@pytest.mark.parametrize(
    ("tokens", "command"),
    [
        ([], ([], [])),
        (["git"], (["git"], [])),
        (["git", "-C"], (["git"], [("-C", "")])),
        (
            ["/usr/bin/git", "--no-pager", "-C", "x", "-c", "a=b", "push", "-u"],
            (["git", "push", "-u"], [("--no-pager", ""), ("-C", "x"), ("-c", "a=b")]),
        ),
        (
            ["git", "--git-dir=d", "--work-tree", "w", "push"],
            (["git", "push"], [("--git-dir=d", ""), ("--work-tree", "w")]),
        ),
        (
            [
                "git",
                "--git-dir",
                "g",
                "--namespace",
                "n",
                "--super-prefix",
                "s",
                "--config-env",
                "e",
                "--attr-source",
                "a",
                "push",
            ],
            (
                ["git", "push"],
                [
                    ("--git-dir", "g"),
                    ("--namespace", "n"),
                    ("--super-prefix", "s"),
                    ("--config-env", "e"),
                    ("--attr-source", "a"),
                ],
            ),
        ),
        (["gh", "-R", "o/r", "run", "rerun"], (["gh", "run", "rerun"], [("-R", "o/r")])),
        (["gh", "--repo=o/r", "-R"], (["gh"], [("--repo=o/r", ""), ("-R", "")])),
        (["kubectl", "-n", "x", "apply"], (["kubectl", "apply"], [("-n", "x")])),
        (["kubectl", "apply", "-n", "x"], (["kubectl", "apply", "-n", "x"], [])),
        (["kubectl", "-n"], (["kubectl"], [("-n", "")])),
        (["helm", "--debug", "-n", "x", "upgrade", "r"], (["helm", "upgrade", "r"], [("--debug", ""), ("-n", "x")])),
        (
            ["argocd", "app", "--grpc-web", "--server", "s", "sync", "a"],
            (["argocd", "app", "sync", "a"], [("--grpc-web", ""), ("--server", "s")]),
        ),
        (["argocd", "app", "-H", "h", "get", "-H", "a"], (["argocd", "app", "get", "-H", "a"], [("-H", "h")])),
        (["gh", "run", "-R", "o/r", "rerun", "5"], (["gh", "run", "rerun", "5"], [("-R", "o/r")])),
        (["gh", "api", "-X", "POST", "x"], (["gh", "api", "-X", "POST", "x"], [])),
        (["ls", "-la"], (["ls", "-la"], [])),
        (
            [">/dev/null", "git", "push", "2>", "&", "1", "origin", "&", ">", "out", "dev>x", "3<", "in", "x"],
            (["git", "push", "origin", "dev", "x"], []),
        ),
    ],
)
@pytest.mark.feature("panic-fix-spare-reads-target")
def test_a_command_reads_without_its_redirections_and_git_global_options(
    tokens: list[str], command: tuple[list[str], list[tuple[str, str]]]
) -> None:
    assert blocker_window._command(tokens) == command


@pytest.mark.parametrize(
    ("words", "targets"),
    [
        ([], None),
        (["kubectl", "apply"], None),
        (["gh", "run", "rerun", "5"], ()),
        (["gh", "workflow", "run", "t.yml"], None),
        (["gh", "workflow", "run", "t.yml", "--ref"], None),
        (["gh", "workflow", "run", "t.yml", "--ref", "a"], ("a",)),
        (["gh", "workflow", "run", "t.yml", "--ref=b"], ("b",)),
        (["gh", "workflow", "run", "t.yml", "-r", "c"], ("c",)),
        (["gh", "workflow", "run", "t.yml", "-r=d"], ("d",)),
        (["gh", "workflow", "run", "t.yml", "-re"], ("e",)),
        (["gh", "workflow", "run", "t.yml", "--ref", "a", "-r", "c"], ("c",)),
        (["gh", "workflow", "run", "-r", "x", "t.yml"], ("x",)),
        (["gh", "workflow", "run", "t.yml", "--ref", "a", "-R", "o/r"], None),
        (["gh", "workflow", "run", "t.yml", "--ref", "a", "-Ro/r"], None),
        (["gh", "workflow", "run", "t.yml", "--ref", "a", "--repo", "o/r"], None),
        (["gh", "workflow", "run", "t.yml", "--ref", "a", "--repo=o/r"], None),
        (["gh", "run", "rerun", "5", "-R", "o/r"], None),
        (["gh", "run", "rerun", "5", "--repository"], ()),
        (["git", "push"], ()),
        (["git", "push", "-u", "origin", "HEAD"], ()),
        (
            ["git", "push", "origin", "+HEAD:refs/heads/f", "f", ":", "d:HEAD", "+g"],
            ("refs/heads/f", "f", "", "HEAD", "g"),
        ),
        (["git", "push", "upstream"], None),
        (["git", "push", "--all"], None),
        (["git", "push", "--branches"], None),
        (["git", "push", "--mirror"], None),
        (["git", "push", "--tags"], None),
        (["git", "push", "--follow-tags"], None),
        (["git", "push", "--prune"], None),
        (["git", "push", "--repo"], None),
        (["git", "push", "--repo=x"], None),
    ],
)
@pytest.mark.feature("panic-fix-spare-reads-target")
def test_a_spared_command_names_the_refs_it_writes(words: list[str], targets: tuple[str, ...] | None) -> None:
    assert blocker_window._targets(words) == targets


@pytest.mark.feature("panic-fix-spare-reads-target")
def test_a_dispatch_without_a_ref_names_an_empty_ref() -> None:
    assert blocker_window._ref(["t.yml"]) == ""
    assert blocker_window._ref(["t.yml", "--ref"]) == ""
    assert blocker_window._ref(["t.yml", "-r", "fix", "-f", "a=b"]) == "fix"


def _git_runs(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[tuple[list[str], dict[str, Any]]]:
    from types import SimpleNamespace

    runs: list[tuple[list[str], dict[str, Any]]] = []

    def run(args: list[str], **kwargs: Any) -> Any:
        runs.append((args, kwargs))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(
        blocker_window, "subprocess", SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError)
    )
    return runs


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_git_read_runs_in_the_session_directory_within_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    runs = _git_runs(monkeypatch, SimpleNamespace(returncode=0, stdout=" fix-branch\n"))
    assert blocker_window._git("/work", "branch", "--show-current") == "fix-branch"
    assert runs == [
        (["git", "branch", "--show-current"], {"cwd": "/work", "capture_output": True, "text": True, "timeout": 5})
    ]


@pytest.mark.parametrize("outcome", [OSError("no git"), subprocess.TimeoutExpired("git", 5)])
@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_git_read_that_cannot_run_reads_empty(monkeypatch: pytest.MonkeyPatch, outcome: BaseException) -> None:
    _git_runs(monkeypatch, outcome)
    assert blocker_window._git("/work", "branch") == ""


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_failed_git_read_reads_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    _git_runs(monkeypatch, SimpleNamespace(returncode=128, stdout="fix-branch"))
    assert blocker_window._git("/work", "branch") == ""


@pytest.mark.feature("panic-fix-branch-pushes")
def test_an_unreadable_fix_reads_as_no_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm.store import SwarmError

    def fix_branch(url: str) -> str:
        assert url == FIX
        raise SwarmError("cannot read")

    monkeypatch.setattr(panic, "fix_branch", fix_branch)
    assert blocker_window._fix_branch(FIX) == ""


def _reads(monkeypatch: pytest.MonkeyPatch, answers: dict[tuple[str, ...], str]) -> list[tuple[str, ...]]:
    asked: list[tuple[str, ...]] = []

    def git(cwd: str, *args: str) -> str:
        assert cwd == "/work"
        asked.append(args)
        return answers[args]

    def fix_branch(url: str) -> str:
        assert url == FIX
        return ""

    monkeypatch.setattr(blocker_window, "_git", git)
    monkeypatch.setattr(blocker_window, "_fix_branch", fix_branch)
    return asked


@pytest.mark.parametrize(("cwd", "fixes"), [("", (FIX,)), ("/work", ())])
@pytest.mark.feature("panic-fix-branch-pushes")
def test_no_directory_or_no_fix_reads_no_git(monkeypatch: pytest.MonkeyPatch, cwd: str, fixes: tuple[str, ...]) -> None:
    asked = _reads(monkeypatch, {})
    assert blocker_window._on_fix_branch(cwd, fixes, ()) is False
    assert asked == []


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_detached_checkout_reads_no_origin_and_stays_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _reads(monkeypatch, {("branch", "--show-current"): ""})
    assert blocker_window._on_fix_branch("/work", (FIX,), ()) is False
    assert asked == [("branch", "--show-current")]


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_checkout_without_an_origin_stays_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _reads(monkeypatch, {("branch", "--show-current"): "fix-branch", ("remote", "get-url", "origin"): ""})
    assert blocker_window._on_fix_branch("/work", (FIX,), ()) is False
    assert asked == [("branch", "--show-current"), ("remote", "get-url", "origin")]


@pytest.mark.feature("panic-fix-branch-pushes")
def test_the_frozen_check_reads_the_branch_in_the_given_directory(
    bound: None, store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []

    def on_fix_branch(cwd: str, fixes: tuple[str, ...], targets: tuple[str, ...]) -> bool:
        seen.append((cwd, fixes, targets))
        return True

    monkeypatch.setattr(blocker_window, "_on_fix_branch", on_fix_branch)
    monkeypatch.setattr(blocker_window, "_routed", lambda cwd: False)
    panic.engage(store, SLUG, "reason", False, [FIX], 0)
    assert blocker_window._frozen("Bash", {"command": "git push"}, "/work") is None
    assert seen == [("/work", (FIX,), ())]


@pytest.mark.parametrize("command", ["git status", "gh run list", "kubectl get pods", "echo push", ""])
def test_panic_lets_other_commands_through(bound: None, store, command):
    panic.engage(store, SLUG, "reason", True, [], 0)
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session") == (None, None)


def test_without_panic_nothing_is_frozen(bound: None, store):
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session") == (None, None)


def test_panic_freezes_only_bash(bound: None, store):
    panic.engage(store, SLUG, "reason", True, [], 0)
    assert blocker_window.pretool("Write", {"command": "git push"}, "engineer-session") == (None, None)


def test_a_frozen_check_fails_open_without_a_store(bound: None, monkeypatch):
    monkeypatch.setattr(blocker_window, "_store", lambda: None)
    assert blocker_window._frozen("Bash", {"command": "git push"}, "") is None


def test_freezes_matches_command_prefixes():
    assert blocker_window._freezes(["git", "push"]) is True
    assert blocker_window._freezes(["git"]) is False
    assert blocker_window._freezes([]) is False
    assert blocker_window._freezes(["argocd", "app", "get"]) is False


def test_panic_found_fails_open_on_a_redis_error(store, monkeypatch):
    from redis.exceptions import RedisError

    def broken(found: RedisStore, slug: str) -> None:
        raise RedisError("down")

    monkeypatch.setattr(panic, "state", broken)
    assert blocker_window._panic_found(store, SLUG, 0) == {}


def _quiet_watch(monkeypatch: pytest.MonkeyPatch, seen: list[bool]) -> None:
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "findings", lambda *args: [])
    monkeypatch.setattr(panic, "tick_pass", lambda store, slug, ok, now, report: seen.append(ok) or [])


def test_a_pending_hive_launch_and_a_full_reservation_fail_no_step(
    store: RedisStore, counted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import timing
    from scripts.swarm.tick import SpawnError

    seen: list[bool] = []
    _quiet_watch(monkeypatch, seen)
    full = "account reservation is full: account=claude:a sessions=1 cap=1"
    pending = SpawnError("hive launch ordered, waiting for its acknowledgement", "pending")
    recorded: dict[str, Any] = {}
    with timing.tick(SLUG, store), timing.pass_record(store, SLUG):
        with spawn_stall.watch(store, SLUG, None, lambda: 9_000, None):
            for error in (pending, SpawnError(full, "unavailable")):
                with pytest.raises(SpawnError), timing.step("scripts.swarm.controller.FencedRuntime.spawn"):
                    raise error
            recorded = timing.PASS.get() or {}
    assert recorded["failed_steps"] == []
    assert seen == [True]
    assert panic.errors_since(store, SLUG, 0) == 0
    assert [call for call in counted if call[0] == panic.METRIC] == []


def test_a_real_launch_failure_still_fails_its_step(
    store: RedisStore, counted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import timing
    from scripts.swarm.tick import SpawnError

    seen: list[bool] = []
    _quiet_watch(monkeypatch, seen)
    with timing.tick(SLUG, store), spawn_stall.watch(store, SLUG, None, lambda: 9_000, None):
        with pytest.raises(SpawnError), timing.step("scripts.swarm.controller.FencedRuntime.spawn"):
            raise SpawnError("herdr refused the pane", "refused")
    assert seen == [False]
    assert panic.errors_since(store, SLUG, 0) == 1


@pytest.mark.parametrize(
    ("status", "detail", "waits"),
    [
        ("pending", "hive launch ordered, waiting for its acknowledgement", True),
        ("unavailable", "account reservation is full: account=claude:a sessions=1 cap=1", True),
        ("unavailable", "no hive has room for the lane", False),
        ("refused", "account reservation is full: account=claude:a sessions=1 cap=1", False),
        ("refused", "herdr refused the pane", False),
    ],
)
def test_only_a_pending_launch_or_a_full_reservation_waits(status: str, detail: str, waits: bool) -> None:
    from scripts.swarm.tick import SpawnError

    assert SpawnError(detail, status).waiting() is waits
