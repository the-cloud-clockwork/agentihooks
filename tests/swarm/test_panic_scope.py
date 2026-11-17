"""Panic holds only the swarm that pressed it: sessions bound to another swarm and unbound sessions keep pushing and
rerunning, and the sweep cancels only the runs on branches of the panicking swarm's agents."""

import json

import pytest

from hooks.context import blocker_window
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
    ]


OWNED = [
    f"{REPO} run 1 on engineer-a1b2c3-0001",
    f"{REPO} run 2 on ci-a1b2c3-0002-2",
    f"{REPO} run 7 on dev",
    f"{REPO} run 9 on engineer-a1b2c3-00012",
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
