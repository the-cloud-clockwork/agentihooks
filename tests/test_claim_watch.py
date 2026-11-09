import subprocess
import time

import pytest

from hooks.context import claim_watch
from scripts.swarm import ledger_client, store
from scripts.swarm_ledger import ledger_workspace

pytestmark = pytest.mark.unit

NAME = "engineer@a1b2c3-0004"
ENV = {
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_AGENT_NAME": NAME,
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_SWARM_LANE": "eng",
}
KEY = f"{store.PREFIX}:sw:claim:t1"
OTHER = "engineer@a1b2c3-0009"


class Pushed:
    def __init__(self, holder: str | None) -> None:
        self.holder = holder
        self.reads: list[str] = []

    def get(self, key: str) -> str | None:
        self.reads.append(key)
        return self.holder


class Down:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def get(self, key: str) -> str | None:
        import redis

        self.reads.append(key)
        raise redis.ConnectionError("refused")


class Spawn:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))


@pytest.fixture
def hive(monkeypatch):
    from scripts.hive import scope

    seen = []

    def member(environ=None):
        seen.append(environ)
        return "hive-3"

    monkeypatch.setattr(scope, "member", member)
    return seen


@pytest.fixture
def hosted(monkeypatch: pytest.MonkeyPatch) -> list:
    from scripts.swarm_ledger.api import client

    reads = []

    class Slow:
        def __init__(self, base: str, credentials: dict, timeout: float = 10) -> None:
            self.timeout = timeout

        def request(self, slug: str, path: str, payload: dict | None = None) -> dict:
            reads.append((slug, path, self.timeout))
            raise TimeoutError("timed out")

        def collection(self, slug: str, path: str) -> list:
            return [self.request(slug, path)]

    module = ledger_client._ledger()
    monkeypatch.setattr(client, "ResourceClient", Slow)
    monkeypatch.setattr(module.ledger_link, "remote", lambda environ=None, slug="": True)
    monkeypatch.setattr(module, "base", lambda slug="": "http://hosted.test")
    monkeypatch.setattr(module, "credentials", lambda slug, service=False: {})
    monkeypatch.setattr(module.time, "sleep", lambda seconds: reads.append(("slept", seconds)))
    return reads


def _check(holder: str | None, env: dict = ENV, now: float = 1000.0) -> tuple:
    redis, spawn = Pushed(holder), Spawn()
    return claim_watch.check(env, redis, now, spawn), redis, spawn


def test_the_watched_key_is_the_task_claim_the_tick_pushes() -> None:
    assert claim_watch.key("sw", "t1") == KEY


def test_a_new_claimant_stops_the_old_one_at_its_next_tool_call() -> None:
    folder = ledger_workspace.folder("sw", "t1")
    folder.mkdir(parents=True)
    (folder / "progress.md").write_text("earlier step\n")

    stop, redis, spawn = _check(OTHER)

    assert stop == claim_watch.STOP.format(task="t1", slug="sw", owner=OTHER)
    assert redis.reads == [KEY]
    assert spawn.calls == [
        (
            ["agentihooks", "swarm", "sw", "--as", NAME, "exit"],
            {
                "start_new_session": True,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]
    assert (folder / "progress.md").read_text() == "earlier step\n" + claim_watch.NOTE.format(
        at="1970-01-01T00:16:40Z", name=NAME, owner=OTHER
    )


def test_the_claimant_itself_keeps_working() -> None:
    stop, _, spawn = _check(NAME)

    assert stop is None
    assert spawn.calls == []
    assert not (ledger_workspace.folder("sw", "t1") / "progress.md").exists()


def test_an_absent_pushed_claim_keeps_the_session() -> None:
    stop, _, spawn = _check(None)

    assert stop == claim_watch.STOP.format(task="t1", slug="sw", owner="")
    assert len(spawn.calls) == 1


@pytest.mark.parametrize(
    "env",
    [
        {**ENV, "AGENTIHOOKS_SWARM_LANE": "master"},
        {**ENV, "AGENTIHOOKS_SWARM_LANE": "plan"},
        {key: value for key, value in ENV.items() if key != "AGENTIHOOKS_SWARM_TASK"},
        {key: value for key, value in ENV.items() if key != "AGENTIHOOKS_AGENT_NAME"},
        {key: value for key, value in ENV.items() if key != "AGENTIHOOKS_SWARM"},
        {**ENV, "AGENTIHOOKS_WORKER_RUN": "1"},
    ],
)
def test_only_a_swarm_worker_session_is_checked(env: dict) -> None:
    stop, redis, spawn = _check(OTHER, env=env)

    assert stop is None
    assert redis.reads == []
    assert spawn.calls == []


def test_the_ci_lane_is_checked() -> None:
    stop, _, _ = _check(OTHER, env={**ENV, "AGENTIHOOKS_SWARM_LANE": "ci"})

    assert stop is not None


@pytest.mark.feature("swarm-reopened-claim-loss")
def test_a_reassigned_claim_is_checked_on_the_next_call_despite_a_cached_owner() -> None:
    redis, spawn = Pushed(NAME), Spawn()

    assert claim_watch.check(ENV, redis, 1000.0, spawn) is None
    redis.holder = OTHER
    assert claim_watch.check(ENV, redis, 1001.0, spawn) == claim_watch.STOP.format(task="t1", slug="sw", owner=OTHER)
    assert redis.reads == [KEY, KEY]
    assert len(spawn.calls) == 1


def test_a_cached_answer_of_another_agent_is_not_reused() -> None:
    assert claim_watch.check(ENV, Pushed(NAME), 1000.0, Spawn()) is None
    successor, spawn = {**ENV, "AGENTIHOOKS_AGENT_NAME": OTHER}, Spawn()
    redis = Pushed(OTHER)

    assert claim_watch.check(successor, redis, 1001.0, spawn) is None
    assert redis.reads == [KEY]


def test_an_unreachable_redis_keeps_the_session_and_is_not_asked_again_inside_the_window() -> None:
    redis, spawn = Down(), Spawn()

    assert claim_watch.check(ENV, redis, 1000.0, spawn) is None
    assert claim_watch.check(ENV, redis, 1001.0, spawn) is None
    assert redis.reads == [KEY]
    assert spawn.calls == []


def test_a_cached_claim_of_another_agent_stops_again_without_a_read_even_in_the_same_second() -> None:
    redis, spawn = Pushed(OTHER), Spawn()
    stop = claim_watch.STOP.format(task="t1", slug="sw", owner=OTHER)

    assert claim_watch.check(ENV, redis, 1000.0, spawn) == stop
    redis.holder = NAME
    assert claim_watch.check(ENV, redis, 1000.0, spawn) == stop
    assert claim_watch.check(ENV, redis, 1000.5, spawn) == stop
    assert redis.reads == [KEY]


def test_the_session_environment_chooses_the_redis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def client(environ: object) -> Pushed:
        seen.append(environ)
        return Pushed(NAME)

    monkeypatch.setattr(claim_watch, "_client", client)

    assert claim_watch.check(ENV, None, 1000.0, Spawn()) is None
    assert seen == [ENV]


def test_no_redis_keeps_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claim_watch, "_client", lambda environ: None)

    assert claim_watch.check(ENV, None, 1000.0, Spawn()) is None


def test_the_client_reads_the_swarm_redis_with_a_fifty_millisecond_bound_and_no_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis

    built = []

    def from_url(url: str, **options: object) -> str:
        built.append((url, options))
        return "client"

    monkeypatch.setattr(redis.Redis, "from_url", from_url)

    assert claim_watch.READ_S <= 0.05
    assert claim_watch._client({**ENV, "AGENTIHOOKS_SWARM_REDIS_URL": "redis://swarm.test:6379/0"}) == "client"
    assert built == [
        (
            "redis://swarm.test:6379/0",
            {
                "decode_responses": True,
                "socket_connect_timeout": claim_watch.READ_S,
                "socket_timeout": claim_watch.READ_S,
            },
        )
    ]


@pytest.mark.feature("swarm-reopened-claim-loss")
def test_a_released_claim_exits_on_the_next_call_despite_a_cached_owner() -> None:
    redis, spawn = Pushed(NAME), Spawn()

    assert claim_watch.check(ENV, redis, 1000.0, spawn) is None
    redis.holder = None
    assert claim_watch.check(ENV, redis, 1001.0, spawn) == claim_watch.STOP.format(task="t1", slug="sw", owner="")
    assert redis.reads == [KEY, KEY]
    assert len(spawn.calls) == 1


@pytest.mark.parametrize("saved", ["[]", "null", '"x"', '{"name": "' + NAME + '", "at": "soon", "holder": null}'])
def test_a_cache_file_of_another_shape_is_a_miss(saved: str) -> None:
    folder = ledger_workspace.folder("sw", "t1")
    folder.mkdir(parents=True)
    (folder / claim_watch.CACHE).write_text(saved)
    redis = Pushed(OTHER)

    assert claim_watch.check(ENV, redis, 1000.0, Spawn()) is not None
    assert redis.reads == [KEY]


def test_the_pre_tool_hook_denies_the_next_call_of_a_replaced_claimant(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks import hook_manager

    redis, spawn = Pushed(OTHER), Spawn()
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_WORKER_RUN", raising=False)
    monkeypatch.setattr(claim_watch, "_client", lambda environ: redis)
    monkeypatch.setattr(claim_watch.subprocess, "Popen", spawn)

    with pytest.raises(hook_manager.BlockAction, match="no longer claimed by you"):
        hook_manager.on_pre_tool_use(
            {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": "/"}
        )
    assert redis.reads == [KEY]
    assert [argv for argv, _ in spawn.calls] == [["agentihooks", "swarm", "sw", "--as", NAME, "exit"]]


@pytest.mark.wall_clock
def test_a_ledger_answering_after_five_seconds_does_not_slow_a_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.context import context_recycle, swarm_gates

    def slow(*args: object, **kwargs: object) -> dict:
        time.sleep(5)
        return {"id": "t1", "state": "claimed", "claimed_by": NAME}

    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_WORKER_RUN", raising=False)
    monkeypatch.setattr(ledger_client.LedgerClient, "task", slow)
    monkeypatch.setattr(ledger_client.LedgerClient, "item", slow)
    monkeypatch.setattr(context_recycle, "gate", lambda *args: None)
    monkeypatch.setattr(claim_watch, "_client", lambda environ: Pushed(NAME))
    started = time.monotonic()

    assert swarm_gates.pretool("Bash", {"command": "ls"}, "s1") is None
    assert time.monotonic() - started < 0.1


def test_a_lost_pushed_claim_still_exits_through_the_swarm_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    from hooks.context import context_recycle, swarm_gates

    spawn = Spawn()
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_WORKER_RUN", raising=False)
    monkeypatch.setattr(context_recycle, "gate", lambda *args: None)
    monkeypatch.setattr(claim_watch, "_client", lambda environ: Pushed(OTHER))
    monkeypatch.setattr(claim_watch.subprocess, "Popen", spawn)

    assert swarm_gates.pretool("Bash", {"command": "ls"}, "s1") == claim_watch.STOP.format(
        task="t1", slug="sw", owner=OTHER
    )
    assert [argv for argv, _ in spawn.calls] == [["agentihooks", "swarm", "sw", "--as", NAME, "exit"]]


@pytest.mark.parametrize("session", [{}, {"session_id": "s1"}])
def test_the_pre_tool_hook_passes_the_session_to_swarm_gates(hive, monkeypatch, session):
    from hooks import hook_manager
    from hooks.context import swarm_gates

    calls = []

    def pretool(tool_name, tool_input, session_id):
        calls.append((tool_name, tool_input, session_id))
        return "stop"

    monkeypatch.setattr(swarm_gates, "pretool", pretool)
    with pytest.raises(hook_manager.BlockAction, match="^stop$"):
        hook_manager.on_pre_tool_use(
            {**session, "tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": "/"}
        )
    assert calls == [("Bash", {"command": "git status"}, session.get("session_id", ""))]


def test_the_swarm_gates_run_in_order_and_the_first_block_wins(monkeypatch):
    from hooks.context import context_recycle, swarm_gates

    calls = []

    def recycle(tool_name, tool_input, session_id):
        calls.append(("recycle", tool_name, tool_input, session_id))
        return "handoff"

    def claim():
        calls.append(("claim",))
        return "stop"

    monkeypatch.setattr(context_recycle, "gate", recycle)
    monkeypatch.setattr(claim_watch, "check", claim)

    assert swarm_gates.pretool("Bash", {"command": "ls"}, "s1") == "stop"
    assert calls == [("claim",)]

    monkeypatch.setattr(claim_watch, "check", lambda: None)
    calls.clear()
    assert swarm_gates.pretool("Bash", {}, "s1") == "handoff"
    assert calls == [("recycle", "Bash", {}, "s1")]


@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError, SystemExit])
def test_a_failing_swarm_gate_is_logged_and_the_next_one_still_runs(monkeypatch, error):
    from hooks.context import context_recycle, swarm_gates

    logged = []

    def broken(*args):
        raise error("no transcript")

    monkeypatch.setattr(context_recycle, "gate", broken)
    monkeypatch.setattr(claim_watch, "check", lambda: None)
    monkeypatch.setattr(swarm_gates, "log", lambda message, payload: logged.append((message, payload)))

    assert swarm_gates.pretool("Bash", {}, "s1") is None
    assert logged == [("context recycle gate failed", {"error": "no transcript"})]

    monkeypatch.setattr(context_recycle, "gate", lambda *args: "handoff")
    monkeypatch.setattr(claim_watch, "check", broken)
    logged.clear()
    assert swarm_gates.pretool("Bash", {}, "s1") == "handoff"
    assert logged == [("claim watch failed", {"error": "no transcript"})]


def test_other_ledger_clients_keep_three_ten_second_attempts(hosted: list) -> None:
    with pytest.raises(TimeoutError):
        ledger_client.LedgerClient().task("sw", "t1")

    assert hosted == [
        ("sw", "tasks/t1", 10),
        ("slept", 0.5),
        ("sw", "tasks/t1", 10),
        ("slept", 1.0),
        ("sw", "tasks/t1", 10),
    ]


def test_a_bounded_read_forwards_the_service_and_collection_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Module:
        @staticmethod
        def resource(slug: str, path: str, **options: object) -> list:
            calls.append((slug, path, options))
            return []

    monkeypatch.setattr(ledger_client, "_ledger", lambda: Module)
    client = ledger_client.LedgerClient(service=True, read_timeout=2)

    assert client.tasks("sw") == []
    assert calls == [
        ("sw", "tasks", {"service": True, "collection": True, "timeout": 2, "attempts": 1}),
    ]


def test_a_bounded_client_reads_collections_once_with_its_timeout(hosted: list) -> None:
    with pytest.raises(TimeoutError):
        ledger_client.LedgerClient(read_timeout=3).tasks("sw")

    assert hosted == [("sw", "tasks", 3)]
