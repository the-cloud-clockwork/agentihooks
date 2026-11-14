import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hooks.context import blocker_window, master_coding
from hooks.observability import otel
from scripts.swarm import cli, panic
from scripts.swarm.health import spawn_stall
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig
from scripts.swarm.tick_watch import STALE_TICK, _alert
from scripts.tick.adapters.fakes import Call, FakeSourceControl
from scripts.tick.adapters.github import GitHubSourceControl
from scripts.tick.deadline import Deadline
from scripts.tick.ports.source_control import RepositoryRun
from scripts.tick.steps import place

pytestmark = pytest.mark.unit

SLUG = "sw"
SID = "master-session"
HOUR = panic.HOUR_MS
WINDOW = panic.WINDOW_MS
FIX = "https://github.com/the-cloud-clockwork/agentihooks/pull/1"


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    found = RedisStore(redis)
    found.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: redis)
    return found


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float, dict[str, str]]]:
    calls: list[tuple[str, float, dict[str, str]]] = []
    monkeypatch.setattr(otel, "add_counter", lambda name, value, attrs: calls.append((name, value, attrs)))
    return calls


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


def _branches(monkeypatch: pytest.MonkeyPatch, branches: dict[str, str]) -> list[str]:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    asked: list[str] = []

    def change(url: str, refused: str) -> ChangeState:
        assert refused == f"cannot read pull request {url}"
        asked.append(url)
        return ChangeState(ChangeRef("o/r", 1), "open", "a" * 40, "dev", False, url, branches[url])

    monkeypatch.setattr(stack, "change", change)
    return asked


def _row(run_id: int, branch: str, status: str = "in_progress", event: str = "pull_request") -> RepositoryRun:
    return RepositoryRun(run_id, branch, event, status)


def _errors(store: RedisStore, count: int, at: int, category: str = "SpawnError") -> None:
    for _ in range(count):
        panic.count_error(store, SLUG, category, at)


def _passes(store: RedisStore, ok: int, bad: int, at: int) -> None:
    for _ in range(ok):
        panic.count_pass(store, True, at)
    for _ in range(bad):
        panic.count_pass(store, False, at)


def test_an_error_lands_on_the_otel_counter_by_swarm_and_category(store, counted):
    panic.count_error(store, SLUG, "SpawnError", 1_000)
    assert counted == [(panic.METRIC, 1, {"swarm": SLUG, "category": "SpawnError"})]
    assert panic.METRIC == "agentihooks.swarm.errors"


def test_the_counter_reads_errors_in_the_last_hour_by_category(store, counted):
    now = 10 * HOUR
    _errors(store, 2, now - HOUR, "SpawnError")
    _errors(store, 1, now - HOUR - 1, "SpawnError")
    _errors(store, 3, now, STALE_TICK)
    assert panic.errors_since(store, now - HOUR) == 5
    assert panic.errors_by_category(store, now - HOUR) == {"SpawnError": 2, STALE_TICK: 3}


def test_the_window_drops_errors_older_than_an_hour(store, counted):
    _errors(store, 1, 0)
    _errors(store, 1, HOUR + 1)
    assert store.redis.zcard(f"{PREFIX}:{panic.FLEET}:errors") == 1


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
    assert panic.errors_by_category(store, 0) == {"RuntimeError": 1}
    assert counted == [(panic.METRIC, 1, {"swarm": SLUG, "category": "RuntimeError"})]


def test_a_tick_pass_records_whether_any_step_failed(store, counted, monkeypatch, capsys):
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "findings", lambda *args: [])
    seen: list[tuple[bool, int]] = []
    monkeypatch.setattr(panic, "tick_pass", lambda store, ok, now: seen.append((ok, now)) or ["x"])
    from scripts.swarm import timing

    with spawn_stall.watch(store, SLUG, None, lambda: 7_000, None):
        pass
    with spawn_stall.watch(store, SLUG, None, lambda: 8_000, None):
        failed = timing.ON_FAILURE.get()
        assert failed is not None
        failed("step", RuntimeError("boom"))
    assert seen == [(True, 7_000), (False, 8_000)]
    line = '{"event": "swarm_panic", "slug": "sw", "action": "x"}'
    assert [row for row in capsys.readouterr().out.splitlines() if "swarm_panic" in row] == [line, line]


def test_the_pass_window_drops_passes_older_than_fifteen_minutes(store):
    panic.count_pass(store, True, 0)
    panic.count_pass(store, True, 1)
    panic.count_pass(store, True, WINDOW + 1)
    assert store.redis.zcard(f"{PREFIX}:{panic.FLEET}:passes-ok") == 2
    panic.count_pass(store, False, 0)
    assert store.redis.zcard(f"{PREFIX}:{panic.FLEET}:passes-failed") == 1


def test_the_error_window_keeps_errors_inside_the_hour(store, counted):
    _errors(store, 1, 1)
    _errors(store, 1, HOUR + 1)
    assert store.redis.zcard(f"{PREFIX}:{panic.FLEET}:errors") == 2


def test_due_cancel_holds_the_cancel_key_for_a_minute(store):
    assert panic.due_cancel(store, 0) is True
    assert panic.due_cancel(store, 1) is False
    ttl = store.redis.pttl(f"{PREFIX}:{panic.FLEET}:cancel")
    assert 0 < ttl <= panic.CANCEL_EVERY_MS


def test_a_stale_tick_counts_an_error(store, counted):
    _alert(store, SLUG, 10_000, None, ["late"])
    assert panic.errors_by_category(store, 0) == {STALE_TICK: 1}
    assert counted == [(panic.METRIC, 1, {"swarm": SLUG, "category": STALE_TICK})]


def test_the_alarm_engages_panic_at_the_threshold(store, counted):
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD - 1, now)
    assert panic.alarm(store, now) == ""
    assert panic.state(store) is None
    _errors(store, 1, now)
    assert panic.alarm(store, now) == f"panic engaged: {panic.THRESHOLD} errors in the last hour"
    assert panic.state(store) == panic.Panic(now, f"{panic.THRESHOLD} errors in the last hour", True, ())


def test_the_threshold_is_sixty_errors_an_hour():
    assert panic.THRESHOLD == 60
    assert panic.RELEASE_LEVEL == 0.9
    assert panic.RELEASE_ERRORS == 15


def test_the_alarm_counts_only_the_last_hour(store, counted):
    now = 3 * HOUR
    _errors(store, panic.THRESHOLD, now - HOUR - 1)
    assert panic.alarm(store, now) == ""


def test_an_automatic_panic_releases_once_stable(store, counted):
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD, now)
    panic.alarm(store, now)
    later = now + WINDOW + 1
    _passes(store, 9, 1, later)
    assert panic.alarm(store, later) == "panic released: the fleet is back at business as usual"
    assert panic.state(store) is None


def test_an_automatic_panic_holds_below_ninety_percent(store, counted):
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD, now)
    panic.alarm(store, now)
    later = now + WINDOW + 1
    _passes(store, 8, 2, later)
    assert panic.level(store, later) == 0.8
    assert panic.alarm(store, later) == ""
    assert panic.state(store) is not None


def test_a_manual_panic_never_releases_itself(store, counted):
    panic.engage(store, "pressed", False, [FIX], 0)
    _passes(store, 10, 0, WINDOW + 1)
    assert panic.alarm(store, WINDOW + 1) == ""
    assert panic.state(store) == panic.Panic(0, "pressed", False, (FIX,))


def test_stable_needs_the_level_and_few_recent_errors(store, counted):
    now = 5 * HOUR
    _passes(store, 9, 1, now)
    _errors(store, panic.RELEASE_ERRORS - 1, now - WINDOW)
    assert panic.stable(store, now) is True
    _errors(store, 1, now)
    assert panic.stable(store, now) is False


def test_level_is_one_with_no_passes_and_ignores_old_ones(store):
    assert panic.level(store, HOUR) == 1.0
    _passes(store, 0, 3, HOUR - WINDOW - 1)
    _passes(store, 1, 0, HOUR - WINDOW)
    assert panic.level(store, HOUR) == 1.0


def test_engage_keeps_the_first_panic_and_adds_fixes(store: RedisStore) -> None:
    panic.engage(store, "auto", True, [], 100)
    found = panic.engage(store, "pressed", False, [FIX, FIX], 200)
    assert found == panic.Panic(100, "auto", True, (FIX,))
    assert panic.state(store) == found


def test_a_fix_added_to_an_automatic_panic_still_releases_once_stable(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]]
) -> None:
    now = 2 * HOUR
    _errors(store, panic.THRESHOLD, now)
    panic.alarm(store, now)
    panic.engage(store, "pressed", False, (fix for fix in [FIX]), now + 1)
    assert panic.state(store) == panic.Panic(now, f"{panic.THRESHOLD} errors in the last hour", True, (FIX,))
    later = now + WINDOW + 1
    _passes(store, 9, 1, later)
    assert panic.alarm(store, later) == "panic released: the fleet is back at business as usual"
    assert panic.state(store) is None


def test_pressing_on_without_a_fix_makes_an_automatic_panic_manual(store: RedisStore) -> None:
    panic.engage(store, "auto", True, [], 100)
    assert panic.engage(store, "pressed", False, [], 200) == panic.Panic(100, "auto", False, ())


def test_a_panic_a_person_turns_on_without_a_fix_stays_manual(
    store: RedisStore, counted: list[tuple[str, float, dict[str, str]]]
) -> None:
    panic.engage(store, "pressed", False, [], 0)
    _passes(store, 10, 0, WINDOW + 1)
    assert panic.alarm(store, WINDOW + 1) == ""
    assert panic.state(store) == panic.Panic(0, "pressed", False, ())


def test_engage_on_an_automatic_panic_stays_automatic(store):
    panic.engage(store, "auto", True, [], 100)
    assert panic.engage(store, "again", True, ["b", "a"], 200) == panic.Panic(100, "auto", True, ("a", "b"))


def test_release_refuses_below_the_level(store, counted):
    panic.engage(store, "pressed", False, [], 0)
    _passes(store, 1, 1, HOUR)
    assert panic.release(store, HOUR) is False
    assert panic.state(store) is not None


def test_cancel_runs_spares_the_merge_queue_the_fix_and_finished_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _branches(monkeypatch, {FIX: "fix-branch"})
    runs = Runs(
        {
            "o/a": [
                _row(1, "feature"),
                _row(2, "fix-branch"),
                _row(3, "gh-readonly-queue/dev/pr-9", event="merge_group"),
                _row(4, "done", status="completed"),
                _row(5, "waiting", status="queued"),
            ],
            "o/b": [_row(6, "other", status="pending")],
        },
    )
    cancelled = panic.cancel_runs([FIX], ["o/a", "o/b"], runs)
    assert cancelled == ["o/a run 1 on feature", "o/a run 5 on waiting", "o/b run 6 on other"]
    assert [(repository, call.operation, call.args) for repository, call in runs.calls()] == [
        ("o/a", "latest_runs", ()),
        ("o/a", "cancel_run", (1,)),
        ("o/a", "cancel_run", (5,)),
        ("o/b", "latest_runs", ()),
        ("o/b", "cancel_run", (6,)),
    ]
    assert all(panic.TOOL_SECONDS - 5 < call.deadline.remaining <= panic.TOOL_SECONDS for _, call in runs.calls())
    assert panic.TOOL_SECONDS == 60.0
    assert asked == [FIX]


def test_every_active_status_is_cancelled():
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        runs = Runs({"o/a": [_row(1, "x", status=status)]})
        assert panic.cancel_runs([], ["o/a"], runs) == ["o/a run 1 on x"]


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
    assert panic.cancel_runs([], ["o/a"], runs) == ["o/a run 3 on v2-feature", "o/a run 4 on release"]
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
    assert panic.cancel_runs([FIX], ["o/a"], runs) == ["o/a run 2 on dev", "o/a run 3 on feature"]


@pytest.mark.feature("panic-merged-fix-dev-run")
def test_cancel_runs_cancels_the_run_on_an_open_fix_merge_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.tick.ports.source_control import ChangeRef, ChangeState

    commit = "m" * 40

    def change(url: str, refused: str) -> ChangeState:
        return ChangeState(ChangeRef("o/r", 1), "open", "a" * 40, "dev", False, url, "fix-branch", merge_commit=commit)

    monkeypatch.setattr(stack, "change", change)
    runs = Runs({"o/a": [RepositoryRun(1, "dev", "push", "in_progress", head_sha=commit)]})
    assert panic.cancel_runs([FIX], ["o/a"], runs) == ["o/a run 1 on dev"]


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
    assert panic.cancel_runs([FIX], ["o/a", "o/b"], runs) == [
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
    assert panic.cancel_runs([FIX, f"{FIX}0"], ["o/a"], runs) == ["o/a run 3 on hotfix", "o/a run 4 on dev"]


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
    assert panic.cancel_runs([FIX], ["o/a", "o/b"], runs) == [
        "o/a run 4 on dev",
        "o/a run 6 on main",
        "o/a run 8 on dev",
        "o/a run 9 on None",
        "o/b run 11 on dev",
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
    runs = Runs({repo: [_row(1, "busy")] for repo in panic.REPOS})
    panic.engage(store, "pressed", False, [], 0)
    first = panic.tick_pass(store, True, 1_000, runs)
    assert first == [f"panic cancelled {repo} run 1 on busy" for repo in panic.REPOS]
    assert panic.tick_pass(store, True, 1_000 + panic.CANCEL_EVERY_MS - 1, runs) == []
    assert len(runs.cancelled()) == len(panic.REPOS)


def test_tick_pass_without_panic_cancels_nothing(store, counted):
    runs = Runs({repo: [_row(1, "busy")] for repo in panic.REPOS})
    assert panic.tick_pass(store, True, 1_000, runs) == []
    assert runs.calls() == []
    assert panic.level(store, 1_000) == 1.0


def test_tick_pass_engages_and_cancels_in_one_pass(store, counted):
    _errors(store, panic.THRESHOLD, 1_000)
    runs = Runs({})
    assert panic.tick_pass(store, False, 1_000, runs) == [f"panic engaged: {panic.THRESHOLD} errors in the last hour"]
    assert panic.level(store, 1_000) == 0.0


def test_tick_pass_reports_a_failed_cancel(store, counted):
    panic.engage(store, "pressed", False, [], 0)

    found = panic.tick_pass(store, True, 1_000, lambda repository: Broken())
    assert len(found) == 1
    assert found[0].startswith("panic cancel failed: ")


def test_tick_pass_reports_an_unreadable_fix_branch(store, counted, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import stack
    from scripts.swarm.store import SwarmError

    panic.engage(store, "pressed", False, [FIX], 0)

    def unreadable(url: str, refused: str) -> None:
        raise SwarmError(f"{refused}: boom")

    monkeypatch.setattr(stack, "change", unreadable)
    assert panic.tick_pass(store, True, 1_000, Runs({})) == [
        f"panic cancel failed: cannot read pull request {FIX}: boom"
    ]


def test_the_tick_spawns_nothing_while_panic_holds():
    source = Path(place.__file__).read_text()
    assert "config.engine != tick.EVENTS_ENGINE and tick.panic.state(store) is None:\n" in source


def test_the_panic_button_cancels_and_prints(store, counted, monkeypatch, capsys):
    _branches(monkeypatch, {FIX: "fix-branch"})
    runs = Runs({repo: [_row(7, "busy"), _row(8, "fix-branch")] for repo in panic.REPOS})
    monkeypatch.setattr(panic, "port", runs)
    args = cli.build_parser().parse_args([SLUG, "panic", "on", "--fix", FIX])
    before = cli.now_ms()
    args.handler(store, args)
    after = cli.now_ms()
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "panic": "on",
        "fixes": [FIX],
        "cancelled": [f"{repo} run 7 on busy" for repo in panic.REPOS],
    }
    current = panic.state(store)
    assert current is not None
    assert current.reason == f"pressed on swarm {SLUG}"
    assert current.auto is False
    assert before <= current.since <= after


def test_the_panic_button_refuses_an_unknown_action():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([SLUG, "panic", "maybe"])


def test_the_panic_button_off_refuses_below_business_as_usual(store, counted):
    panic.engage(store, "pressed", False, [], 0)
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
    panic.engage(store, "pressed", False, [], 0)
    args = cli.build_parser().parse_args([SLUG, "panic", "off"])
    args.handler(store, args)
    assert json.loads(capsys.readouterr().out) == {"panic": "off"}
    assert panic.state(store) is None


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
    panic.engage(store, "pressed", False, [FIX], 5)
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
    assert json.loads(capsys.readouterr().out) == {"panic": "on", "fixes": [], "cancelled": []}


def test_panic_found_names_the_panic_since_its_start(store):
    from scripts.swarm.blockers import Blocker

    panic.engage(store, "reason", True, [], 3)
    assert blocker_window._panic_found(store, 5) == {"panic": Blocker("panic", 3, 5, 0, "reason")}


def test_the_panic_notice_is_rate_limited_per_swarm(store, master):
    panic.engage(store, "reason", True, [], blocker_window._now_ms(store))
    blocker_window.pretool("Read", {}, SID)
    assert store.redis.get(store.key(SLUG, "blockers-notice")) is not None


@pytest.fixture
def master(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    master_coding.record_seat(SID, SLUG, "master@sw")
    return tmp_path


@pytest.mark.feature("panic-master-keeps-every-tool")
def test_panic_opens_the_master_hotfix_window(store, master):
    panic.engage(store, "60 errors in the last hour", True, [], blocker_window._now_ms(store))
    notice, block = blocker_window.pretool("Write", {"file_path": "/etc/x"}, SID)
    assert master_coding.is_open(SID)
    assert notice is not None
    assert "panic for 0 minutes" in notice
    assert f"agentihooks swarm {SLUG} panic on --fix <pr url>" in notice
    assert block is None


def test_the_master_may_press_the_panic_button(store, master):
    panic.engage(store, "pressed", False, [], blocker_window._now_ms(store))
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
def test_panic_freezes_pushes_reruns_dispatches_and_deploys_for_other_sessions(store, command):
    panic.engage(store, "60 errors in the last hour", True, [], 0)
    notice, block = blocker_window.pretool("Bash", {"command": command}, "engineer-session")
    assert notice is None
    assert block == blocker_window.FROZEN.format(reason="60 errors in the last hour")


def _checkout(root: Path, branch: str, origin: str = "git@github.com:The-Cloud-Clockwork/agentihooks.git") -> str:
    subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    return str(root)


@pytest.mark.parametrize("command", ["git push origin fix-branch", "gh run rerun 5", "gh workflow run t.yml"])
@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_spares_a_push_from_the_head_branch_of_a_registered_fix(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    asked = _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "60 errors in the last hour", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd) == (None, None)
    assert asked == [FIX]


@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_still_refuses_a_push_from_any_other_branch(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "60 errors in the last hour", False, [FIX], 0)
    cwd = _checkout(tmp_path / "other", "other-branch")
    block = blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", cwd)[1]
    assert block == blocker_window.FROZEN.format(reason="60 errors in the last hour")


@pytest.mark.feature("panic-fix-branch-pushes")
def test_the_hook_payload_cwd_decides_the_fix_branch_push(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "60 errors in the last hour", False, [FIX], 0)
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "session_id": "engineer-session"}
    fix = _checkout(tmp_path / "fix", "fix-branch")
    other = _checkout(tmp_path / "other", "other-branch")
    assert blocker_window.checked({**payload, "cwd": fix}) is None
    assert blocker_window.checked({**payload, "cwd": other}) is not None
    assert blocker_window.checked(payload) is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_push_after_cd_into_the_fix_worktree_is_spared(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "reason", False, [FIX], 0)
    primary = _checkout(tmp_path / "primary", "dev")
    fix = _checkout(tmp_path / "fix", "fix-branch")
    other = _checkout(tmp_path / "other", "other-branch")
    spared = blocker_window.pretool("Bash", {"command": f"cd {fix} && git push -u origin HEAD"}, "s", primary)
    refused = blocker_window.pretool("Bash", {"command": f"cd {other} && git push"}, "s", fix)
    assert spared == (None, None)
    assert refused[1] is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_push_without_a_session_directory_stays_frozen(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "reason", False, [FIX], 0)
    fix = _checkout(tmp_path / "fix", "fix-branch")
    refused = blocker_window.pretool("Bash", {"command": f"cd {fix} && git push"}, "s", "")
    assert refused[1] is not None
    assert blocker_window._directory("", [str(fix)]) == ""


@pytest.mark.parametrize("command", ["kubectl apply -f a.yaml", "git push && helm upgrade x y"])
def test_a_fix_branch_session_still_waits_to_deploy(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "reason", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session", cwd)[1] is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_same_named_branch_in_another_repository_stays_frozen(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked = _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "reason", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch", "https://github.com/the-cloud-clockwork/antoncore.git")
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", cwd)[1] is not None
    assert asked == []


@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_refuses_a_push_when_the_fix_cannot_be_read(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm import stack
    from scripts.swarm.store import SwarmError

    def change(url: str, refused: str) -> None:
        raise SwarmError(refused)

    monkeypatch.setattr(stack, "change", change)
    panic.engage(store, "reason", False, [FIX], 0)
    cwd = _checkout(tmp_path / "fix", "fix-branch")
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", cwd)[1] is not None


@pytest.mark.feature("panic-fix-branch-pushes")
def test_panic_refuses_a_push_outside_a_git_checkout(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _branches(monkeypatch, {FIX: "fix-branch"})
    panic.engage(store, "reason", False, [FIX], 0)
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session", str(tmp_path))[1] is not None


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
    assert blocker_window._on_fix_branch(cwd, fixes) is False
    assert asked == []


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_detached_checkout_reads_no_origin_and_stays_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _reads(monkeypatch, {("branch", "--show-current"): ""})
    assert blocker_window._on_fix_branch("/work", (FIX,)) is False
    assert asked == [("branch", "--show-current")]


@pytest.mark.feature("panic-fix-branch-pushes")
def test_a_checkout_without_an_origin_stays_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _reads(monkeypatch, {("branch", "--show-current"): "fix-branch", ("remote", "get-url", "origin"): ""})
    assert blocker_window._on_fix_branch("/work", (FIX,)) is False
    assert asked == [("branch", "--show-current"), ("remote", "get-url", "origin")]


@pytest.mark.feature("panic-fix-branch-pushes")
def test_the_frozen_check_reads_the_branch_in_the_given_directory(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, tuple[str, ...]]] = []

    def on_fix_branch(cwd: str, fixes: tuple[str, ...]) -> bool:
        seen.append((cwd, fixes))
        return True

    monkeypatch.setattr(blocker_window, "_on_fix_branch", on_fix_branch)
    panic.engage(store, "reason", False, [FIX], 0)
    assert blocker_window._frozen("Bash", {"command": "git push"}, "/work") is None
    assert seen == [("/work", (FIX,))]


@pytest.mark.parametrize("command", ["git status", "gh run list", "kubectl get pods", "echo push", ""])
def test_panic_lets_other_commands_through(store, command):
    panic.engage(store, "reason", True, [], 0)
    assert blocker_window.pretool("Bash", {"command": command}, "engineer-session") == (None, None)


def test_without_panic_nothing_is_frozen(store):
    assert blocker_window.pretool("Bash", {"command": "git push"}, "engineer-session") == (None, None)


def test_panic_freezes_only_bash(store):
    panic.engage(store, "reason", True, [], 0)
    assert blocker_window.pretool("Write", {"command": "git push"}, "engineer-session") == (None, None)


def test_a_frozen_check_fails_open_without_a_store(monkeypatch):
    monkeypatch.setattr(blocker_window, "_store", lambda: None)
    assert blocker_window._frozen("Bash", {"command": "git push"}, "") is None


def test_freezes_matches_command_prefixes():
    assert blocker_window._freezes(["git", "push"]) is True
    assert blocker_window._freezes(["git"]) is False
    assert blocker_window._freezes([]) is False
    assert blocker_window._freezes(["argocd", "app", "get"]) is False


def test_panic_found_fails_open_on_a_redis_error(store, monkeypatch):
    from redis.exceptions import RedisError

    def broken(found: RedisStore) -> None:
        raise RedisError("down")

    monkeypatch.setattr(panic, "state", broken)
    assert blocker_window._panic_found(store, 0) == {}


def _quiet_watch(monkeypatch: pytest.MonkeyPatch, seen: list[bool]) -> None:
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall, "findings", lambda *args: [])
    monkeypatch.setattr(panic, "tick_pass", lambda store, ok, now: seen.append(ok) or [])


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
    assert panic.errors_since(store, 0) == 0
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
    assert panic.errors_since(store, 0) == 1


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
