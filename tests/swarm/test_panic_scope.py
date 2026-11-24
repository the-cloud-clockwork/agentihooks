"""Panic holds only the swarm that pressed it: sessions bound to another swarm and unbound sessions keep pushing and
rerunning, and the sweep cancels only the runs on branches of the panicking swarm's agents."""

import json
from pathlib import Path

import pytest

from hooks.context import blocker_window, master_coding
from hooks.observability import otel
from scripts.swarm import cli, panic
from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig
from scripts.tick.adapters.fakes import FakeSourceControl
from scripts.tick.ports.source_control import RepositoryRun

pytestmark = pytest.mark.unit

HELD = "held"
OTHER = "other"
CODES = {HELD: "a1b2c3", OTHER: "d4e5f6"}
REPO = "the-cloud-clockwork/agentihooks"
COMMANDS = ("git push", "git push origin HEAD", "gh run rerun 5", "gh workflow run t.yml")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RedisStore:
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    found = RedisStore(redis)
    for slug, code in CODES.items():
        found.names.mint = lambda code=code: code
        found.create(SwarmConfig(slug, "/repo", max_eng=1, max_ci=1))
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda environ=None: redis)
    monkeypatch.setattr(otel, "add_counter", lambda name, value, attrs: None)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    return found


class Runs:
    def __init__(self, rows: list[RepositoryRun]) -> None:
        self.rows = rows
        self.made: dict[str, FakeSourceControl] = {}

    def __call__(self, repository: str) -> FakeSourceControl:
        answers: dict[str, object] = {"latest_runs": tuple(self.rows) if repository == REPO else ()}
        return self.made.setdefault(repository, FakeSourceControl(answers))


def _press(store: RedisStore, monkeypatch: pytest.MonkeyPatch, runs: Runs, slug: str) -> None:
    monkeypatch.setattr(panic, "port", runs)
    args = cli.build_parser().parse_args([slug, "panic", "on"])
    args.handler(store, args)


def _rows() -> list[RepositoryRun]:
    return [
        RepositoryRun(1, "engineer-a1b2c3-0001", "pull_request", "in_progress"),
        RepositoryRun(2, "ci-a1b2c3-0002-2", "push", "queued"),
        RepositoryRun(3, "engineer-d4e5f6-0001", "pull_request", "in_progress"),
        RepositoryRun(4, "session-0123abcd", "pull_request", "in_progress"),
        RepositoryRun(5, "feature", "pull_request", "in_progress"),
        RepositoryRun(6, "dev", "push", "in_progress"),
        RepositoryRun(7, "dev", "workflow_run", "in_progress", title="Gate Required for engineer-a1b2c3-0001"),
        RepositoryRun(8, "dev", "workflow_run", "in_progress", title="Gate Required for engineer-d4e5f6-0001"),
        RepositoryRun(9, "engineer-a1b2c3-00012", "pull_request", "in_progress"),
        RepositoryRun(10, "xengineer-a1b2c3-0001", "pull_request", "in_progress"),
        RepositoryRun(11, "diffcheck/engineer-a1b2c3-0001-red", "push", "in_progress"),
        RepositoryRun(12, "engineer-a1b2c3-0001x", "pull_request", "in_progress"),
        RepositoryRun(13, "dev", "push", "in_progress", title="Tests for engineer-a1b2c3-0001"),
        RepositoryRun(14, None, "schedule", "in_progress"),
    ]


def _release_rows() -> list[RepositoryRun]:
    return [
        RepositoryRun(21, "main", "workflow_run", "in_progress", title="Helm chart release"),
        RepositoryRun(22, "main", "workflow_dispatch", "queued", title="Publish to PyPI"),
        RepositoryRun(23, "main", "workflow_dispatch", "in_progress", title="Release"),
        RepositoryRun(24, "main", "push", "in_progress", title="Promote green dev to main (#12957)"),
        RepositoryRun(25, "v2.17.1108", "release", "in_progress", title="Swarm worker image"),
        RepositoryRun(26, "engineer-a1b2c3-0001", "pull_request", "in_progress"),
        RepositoryRun(27, "main", "workflow_run", "in_progress", title="Gate Required status for engineer-a1b2c3-0001"),
    ]


OWNED = [
    f"{REPO} run 1 on engineer-a1b2c3-0001",
    f"{REPO} run 2 on ci-a1b2c3-0002-2",
    f"{REPO} run 7 on dev",
    f"{REPO} run 9 on engineer-a1b2c3-00012",
    f"{REPO} run 11 on diffcheck/engineer-a1b2c3-0001-red",
]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.feature("panic-holds-only-its-swarm")
def test_panic_on_one_swarm_leaves_other_swarms_and_unbound_sessions_free(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _press(store, monkeypatch, Runs([]), HELD)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", OTHER)
    assert blocker_window.pretool("Bash", {"command": command}, "other-session") == (None, None)
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    assert blocker_window.pretool("Bash", {"command": command}, "unbound-session") == (None, None)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", HELD)
    frozen = blocker_window.FROZEN.format(slug=HELD, reason=f"pressed on swarm {HELD}")
    assert blocker_window.pretool("Bash", {"command": command}, "held-session") == (None, frozen)


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_the_panic_button_cancels_only_the_runs_of_its_swarms_agents(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _press(store, monkeypatch, Runs(_rows()), HELD)
    assert json.loads(capsys.readouterr().out)["cancelled"] == OWNED


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_a_legacy_fleet_panic_holds_only_the_swarm_its_reason_names(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = {"since": 5, "reason": f"pressed on swarm {HELD}", "auto": False, "fixes": []}
    store.redis.set(f"{PREFIX}:fleet:panic", json.dumps(legacy))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", OTHER)
    assert blocker_window.pretool("Bash", {"command": "git push"}, "other-session") == (None, None)
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    assert blocker_window.pretool("Bash", {"command": "git push"}, "unbound-session") == (None, None)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", HELD)
    frozen = blocker_window.FROZEN.format(slug=HELD, reason=f"pressed on swarm {HELD}")
    assert blocker_window.pretool("Bash", {"command": "git push"}, "held-session") == (None, frozen)


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_the_tick_sweep_cancels_only_the_runs_of_the_held_swarms_agents(store: RedisStore) -> None:
    runs = Runs(_rows())
    reported: list[str] = []
    panic.engage(store, HELD, "pressed", False, [], 0)
    assert panic.tick_pass(store, OTHER, True, 1_000, reported.append, runs) == []
    panic.settle()
    assert runs.made == {}
    assert panic.tick_pass(store, HELD, True, 1_000, reported.append, runs) == []
    panic.settle()
    assert reported == [f"panic cancelled {line}" for line in OWNED]


@pytest.mark.feature("panic-sweep-spares-release-runs")
def test_the_panic_button_leaves_release_runs_on_main_running(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = Runs(_release_rows())
    _press(store, monkeypatch, runs, HELD)
    assert json.loads(capsys.readouterr().out)["cancelled"] == [
        f"{REPO} run 26 on engineer-a1b2c3-0001",
        f"{REPO} run 27 on main",
    ]
    assert [call.args for call in runs.made[REPO].calls if call.operation == "cancel_run"] == [(26,), (27,)]


@pytest.mark.feature("panic-sweep-spares-release-runs")
def test_the_tick_sweep_leaves_release_runs_on_main_running(store: RedisStore) -> None:
    runs = Runs(_release_rows())
    reported: list[str] = []
    panic.engage(store, HELD, "pressed", False, [], 0)
    assert panic.tick_pass(store, HELD, True, 1_000, reported.append, runs) == []
    panic.settle()
    assert reported == [
        f"panic cancelled {REPO} run 26 on engineer-a1b2c3-0001",
        f"panic cancelled {REPO} run 27 on main",
    ]
    assert [call.args for call in runs.made[REPO].calls if call.operation == "cancel_run"] == [(26,), (27,)]


@pytest.mark.parametrize(
    ("branch", "owned"),
    [
        ("engineer-a1b2c3-0001", True),
        ("master-a1b2c3-0001", True),
        ("ci-a1b2c3-0002-2", True),
        ("planner-a1b2c3-0003", True),
        ("dispatcher-a1b2c3-0004", True),
        ("engineer-a1b2c3-0001-tmp-1", True),
        ("diffcheck/engineer-a1b2c3-0001-red", True),
        ("engineer-a1b2c3-0001/nested", True),
        ("engineer-d4e5f6-0001", False),
        ("engineer-a1b2c3-001", False),
        ("engineer-a1b2c3-0001x", False),
        ("xengineer-a1b2c3-0001", False),
        ("operator-a1b2c3-0001", False),
        ("engineer@a1b2c3-0001", False),
        ("session-0123abcd", False),
        ("promotion/a1b2c3", False),
        ("dev", False),
    ],
)
@pytest.mark.feature("panic-holds-only-its-swarm")
def test_the_owner_names_only_branches_of_the_swarms_agents(store: RedisStore, branch: str, owned: bool) -> None:
    assert panic.owner(store, HELD)(branch) is owned


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_a_swarm_without_a_code_owns_no_branch(store: RedisStore) -> None:
    store.redis.hset(store.key(HELD, "config"), "code", "")
    owns = panic.owner(store, HELD)
    assert (owns("engineer--0001"), owns("engineer-a1b2c3-0001")) == (False, False)


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_one_swarms_errors_and_failed_passes_reach_only_its_own_panic(store: RedisStore) -> None:
    for _ in range(panic.THRESHOLD):
        panic.count_error(store, HELD, "SpawnError", 1_000)
    panic.count_pass(store, HELD, False, 1_000)
    assert (panic.level(store, OTHER, 1_000), panic.level(store, HELD, 1_000)) == (1.0, 0.0)
    assert panic.errors_by_category(store, OTHER, 0) == {}
    assert panic.alarm(store, OTHER, 1_000) == ""
    assert panic.state(store, OTHER) is None
    assert panic.alarm(store, HELD, 1_000) == f"panic engaged: {panic.THRESHOLD} errors in the last hour"
    assert panic.state(store, OTHER) is None


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_each_swarm_keeps_its_own_cancel_minute(store: RedisStore) -> None:
    assert (panic.due_cancel(store, HELD, 0), panic.due_cancel(store, OTHER, 0)) == (True, True)
    assert (panic.due_cancel(store, HELD, 1), panic.due_cancel(store, OTHER, 1)) == (False, False)


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_panic_off_clears_the_legacy_fleet_record_only_for_the_swarm_it_names(store: RedisStore) -> None:
    key = f"{PREFIX}:fleet:panic"
    store.redis.set(key, json.dumps({"since": 5, "reason": f"pressed on swarm {HELD}", "auto": False, "fixes": []}))
    assert panic.release(store, OTHER, 1_000)
    assert panic.state(store, HELD) == panic.Panic(5, f"pressed on swarm {HELD}", False, ())
    assert panic.release(store, HELD, 1_000)
    assert store.redis.get(key) is None
    assert panic.state(store, HELD) is None


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_panic_off_clears_only_its_own_swarms_kept_fixes(store: RedisStore) -> None:
    kept = {slug: f"{PREFIX}:{slug}:panic:{panic.MERGED_FIXES}" for slug in CODES}
    for key in kept.values():
        store.redis.hset(key, "fix", "{}")
    assert panic.release(store, OTHER, 1_000)
    assert (store.redis.exists(kept[HELD]), store.redis.exists(kept[OTHER])) == (1, 0)


@pytest.mark.parametrize(
    "reason", ["60 errors in the last hour", f"pressed on swarm {HELD} again", "pressed on swarm "]
)
@pytest.mark.feature("panic-holds-only-its-swarm")
def test_a_legacy_fleet_record_naming_no_swarm_holds_none(store: RedisStore, reason: str) -> None:
    store.redis.set(f"{PREFIX}:fleet:panic", json.dumps({"since": 5, "reason": reason, "auto": True, "fixes": []}))
    assert (panic.state(store, HELD), panic.state(store, OTHER)) == (None, None)


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_an_unbound_session_never_reads_panic(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def unread() -> None:
        pytest.fail("an unbound session read the panic store")

    monkeypatch.setattr(blocker_window, "_store", unread)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    assert blocker_window.pretool("Bash", {"command": "git push"}, "unbound-session") == (None, None)


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_swarm_publish_holds_only_the_panicking_swarms_push(store: RedisStore, tmp_path: Path) -> None:
    panic.engage(store, HELD, "pressed", False, [], 0)
    assert blocker_window.held_push(str(tmp_path), "engineer-d4e5f6-0001", OTHER) is None
    frozen = blocker_window.FROZEN.format(slug=HELD, reason="pressed")
    assert blocker_window.held_push(str(tmp_path), "engineer-a1b2c3-0001", HELD) == frozen


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_another_swarms_panic_opens_no_window_for_this_master(
    store: RedisStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.blockers import Blocker

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    master_coding.record_seat("other-master", OTHER, f"master@{CODES[OTHER]}-0001")
    panic.engage(store, HELD, "pressed", False, [], 3)
    assert blocker_window.pretool("Write", {"file_path": "/etc/x"}, "other-master") == (None, None)
    assert not master_coding.is_open("other-master")
    assert blocker_window._panic_found(store, OTHER, 5) == {}
    assert blocker_window._panic_found(store, HELD, 5) == {"panic": Blocker("panic", 3, 5, 0, "pressed")}


@pytest.mark.feature("panic-holds-only-its-swarm")
def test_the_panic_messages_name_the_held_swarm() -> None:
    from scripts.gates.push_stop import PANIC_HELD

    assert blocker_window.FROZEN.format(slug=HELD, reason="pressed") == (
        "BLOCKED: panic mode holds swarm held (pressed). Its sessions' pushes, reruns, dispatches and deploys wait "
        "until the master's fix brings it back to business as usual; agentihooks swarm held panic status shows the "
        "level."
    )
    assert blocker_window.NOTICE.format(slug=HELD, categories="panic for 0 minutes") == (
        "BLOCKER QUEUE on swarm held: panic for 0 minutes sustained for an hour or more. Fix this now: the master "
        "coding window is open; troubleshoot and fix before any other work, and clear each category with "
        "agentihooks swarm held blockers clear <category>. Stop everything unnecessary for the fix: agentihooks "
        "swarm held panic on --fix <pr url> pauses the swarm's agent starts, cancels every CI run of its agents "
        "but the fix and the merge queue, and pauses the lab's pull request grading but the fix, printing what it "
        "cancelled and paused."
    )
    assert PANIC_HELD.format(path="/w") == "Swarm panic holds, so the stop hook pushes nothing and did not push /w."
