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
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        if argv[:3] == ["gh", "run", "list"]:
            return json.dumps(self.rows.get(argv[4], []))
        return ""

    def cancelled(self) -> list[str]:
        return [f"{argv[5]}:{argv[3]}" for argv in self.calls if argv[:3] == ["gh", "run", "cancel"]]


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


def _row(run_id: int, branch: str, status: str = "in_progress", event: str = "pull_request") -> dict[str, Any]:
    return {"databaseId": run_id, "headBranch": branch, "status": status, "event": event}


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
        failed("scripts.swarm.tick._spawn", RuntimeError("boom"))
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


def test_engage_keeps_the_first_panic_and_adds_fixes(store):
    panic.engage(store, "auto", True, [], 100)
    found = panic.engage(store, "pressed", False, [FIX, FIX], 200)
    assert found == panic.Panic(100, "auto", False, (FIX,))
    assert panic.state(store) == found


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
    assert [argv for argv in runs.calls if argv[:3] == ["gh", "run", "cancel"]] == [
        ["gh", "run", "cancel", "1", "--repo", "o/a"],
        ["gh", "run", "cancel", "5", "--repo", "o/a"],
        ["gh", "run", "cancel", "6", "--repo", "o/b"],
    ]
    assert asked == [FIX]
    assert not [argv for argv in runs.calls if argv[:3] == ["gh", "pr", "view"]]
    listing = ["gh", "run", "list", "--repo", "o/a", "--limit", "100", "--json", "databaseId,event,headBranch,status"]
    assert listing in runs.calls


def test_every_active_status_is_cancelled():
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        runs = Runs({"o/a": [_row(1, "x", status=status)]})
        assert panic.cancel_runs([], ["o/a"], runs) == ["o/a run 1 on x"]


def test_the_fleet_repos_include_hooks_bundle_and_antoncore():
    assert panic.REPOS == (
        "the-cloud-clockwork/agentihooks",
        "the-cloud-clockwork/agentihooks-bundle",
        "the-cloud-clockwork/antoncore",
    )


def test_gh_runs_the_command_and_returns_stdout(monkeypatch):
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="out", stderr="")

    monkeypatch.setattr(panic.subprocess, "run", run)
    assert panic.gh(["gh", "x"]) == "out"
    assert seen == {"argv": ["gh", "x"], "capture_output": True, "text": True, "check": True, "timeout": 60}


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
    assert runs.calls == []
    assert panic.level(store, 1_000) == 1.0


def test_tick_pass_engages_and_cancels_in_one_pass(store, counted):
    _errors(store, panic.THRESHOLD, 1_000)
    runs = Runs({})
    assert panic.tick_pass(store, False, 1_000, runs) == [f"panic engaged: {panic.THRESHOLD} errors in the last hour"]
    assert panic.level(store, 1_000) == 0.0


def test_tick_pass_reports_a_failed_cancel(store, counted):
    panic.engage(store, "pressed", False, [], 0)

    def broken(argv: list[str]) -> str:
        raise subprocess.CalledProcessError(1, argv)

    found = panic.tick_pass(store, True, 1_000, broken)
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
    source = Path(panic.__file__).with_name("tick.py").read_text()
    assert "config.engine != EVENTS_ENGINE and panic.state(store) is None:\n" in source


def test_the_panic_button_cancels_and_prints(store, counted, monkeypatch, capsys):
    _branches(monkeypatch, {FIX: "fix-branch"})
    runs = Runs({repo: [_row(7, "busy"), _row(8, "fix-branch")] for repo in panic.REPOS})
    monkeypatch.setattr(panic, "gh", runs)
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
    monkeypatch.setattr(panic, "gh", runs)
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


def test_panic_opens_the_master_hotfix_window(store, master):
    panic.engage(store, "60 errors in the last hour", True, [], blocker_window._now_ms(store))
    notice, block = blocker_window.pretool("Write", {"file_path": "/etc/x"}, SID)
    assert master_coding.is_open(SID)
    assert notice is not None
    assert "panic for 0 minutes" in notice
    assert f"agentihooks swarm {SLUG} panic on --fix <pr url>" in notice
    assert block is not None


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
def test_panic_freezes_pushes_reruns_dispatches_and_deploys_for_other_sessions(store, command):
    panic.engage(store, "60 errors in the last hour", True, [], 0)
    notice, block = blocker_window.pretool("Bash", {"command": command}, "engineer-session")
    assert notice is None
    assert block == blocker_window.FROZEN.format(reason="60 errors in the last hour")


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
    assert blocker_window._frozen("Bash", {"command": "git push"}) is None


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
