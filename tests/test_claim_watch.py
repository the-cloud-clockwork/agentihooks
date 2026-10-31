import subprocess

import pytest

from hooks.context import claim_watch
from scripts.swarm import ledger_client
from scripts.swarm_ledger import ledger_workspace

pytestmark = pytest.mark.unit

NAME = "engineer@a1b2c3-0004"
ENV = {
    "AGENTIHOOKS_SWARM": "sw",
    "AGENTIHOOKS_AGENT_NAME": NAME,
    "AGENTIHOOKS_SWARM_TASK": "t1",
    "AGENTIHOOKS_SWARM_LANE": "eng",
}


class Ledger:
    def __init__(self, row):
        self.row = row
        self.reads = []

    def task(self, slug, task_id):
        self.reads.append((slug, task_id))
        return self.row


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


def _check(row, env=ENV, now=1000.0):
    ledger, spawn = Ledger(row), Spawn()
    return claim_watch.check(env, ledger, now, spawn), ledger, spawn


def test_a_reopened_task_stops_its_old_hive_claimant_at_its_next_tool_call(hive):
    stop, ledger, spawn = _check({"id": "t1", "state": "open", "claimed_by": ""})

    assert stop == claim_watch.STOP.format(task="t1", slug="sw", owner="nobody")
    assert ledger.reads == [("sw", "t1")]
    assert hive == [ENV]
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
    progress = (ledger_workspace.folder("sw", "t1") / "progress.md").read_text()
    assert progress == claim_watch.NOTE.format(at="1970-01-01T00:16:40Z", name=NAME, owner="nobody")


def test_a_new_claimant_stops_the_old_one_and_the_note_names_it(hive):
    folder = ledger_workspace.folder("sw", "t1")
    folder.mkdir(parents=True)
    (folder / "progress.md").write_text("earlier step\n")

    stop, _, spawn = _check({"id": "t1", "state": "claimed", "claimed_by": "engineer@a1b2c3-0009"})

    assert stop == claim_watch.STOP.format(task="t1", slug="sw", owner="engineer@a1b2c3-0009")
    assert len(spawn.calls) == 1
    assert (folder / "progress.md").read_text() == "earlier step\n" + claim_watch.NOTE.format(
        at="1970-01-01T00:16:40Z", name=NAME, owner="engineer@a1b2c3-0009"
    )


def test_the_claimant_itself_keeps_working(hive):
    stop, _, spawn = _check({"id": "t1", "state": "claimed", "claimed_by": NAME})

    assert stop is None
    assert spawn.calls == []
    assert not (ledger_workspace.folder("sw", "t1") / "progress.md").exists()


def test_a_workstation_worker_keeps_an_emptied_claim_the_tick_restores(monkeypatch):
    from scripts.hive import scope

    monkeypatch.setattr(scope, "member", lambda environ=None: "")
    stop, _, spawn = _check({"id": "t1", "state": "open", "claimed_by": ""})

    assert stop is None
    assert spawn.calls == []


def test_a_missing_task_row_keeps_the_session(hive):
    stop, _, spawn = _check(None)

    assert stop is None
    assert spawn.calls == []


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
def test_only_a_swarm_worker_session_is_checked(hive, env):
    stop, ledger, spawn = _check({"id": "t1", "state": "open", "claimed_by": ""}, env=env)

    assert stop is None
    assert ledger.reads == []
    assert spawn.calls == []


def test_the_ci_lane_is_checked(hive):
    stop, _, _ = _check({"id": "t1", "state": "open", "claimed_by": ""}, env={**ENV, "AGENTIHOOKS_SWARM_LANE": "ci"})

    assert stop is not None


def test_a_reopened_task_stops_on_the_call_after_a_successful_check(hive):
    row = {"id": "t1", "state": "claimed", "claimed_by": NAME}
    ledger, spawn = Ledger(row), Spawn()

    assert claim_watch.check(ENV, ledger, 1000.0, spawn) is None
    row.update(state="open", claimed_by="")
    assert claim_watch.check(ENV, ledger, 1001.0, spawn) == claim_watch.STOP.format(
        task="t1", slug="sw", owner="nobody"
    )
    assert ledger.reads == [("sw", "t1"), ("sw", "t1")]
    assert len(spawn.calls) == 1


def test_the_pre_tool_hook_denies_the_next_call_of_a_reopened_claimant(hive, monkeypatch):
    from hooks import hook_manager

    ledger, spawn = Ledger({"id": "t1", "state": "open", "claimed_by": ""}), Spawn()
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_WORKER_RUN", raising=False)
    monkeypatch.setattr("scripts.swarm.ledger_client.LedgerClient.task", lambda _, slug, task: ledger.task(slug, task))
    monkeypatch.setattr(claim_watch.subprocess, "Popen", spawn)

    with pytest.raises(hook_manager.BlockAction, match="no longer claimed by you"):
        hook_manager.on_pre_tool_use(
            {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": "/"}
        )
    assert ledger.reads == [("sw", "t1")]
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


def test_a_slow_hosted_ledger_costs_one_short_read_and_the_call_passes(
    hive: list, hosted: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hooks.context import context_recycle, swarm_gates

    logged = []
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_WORKER_RUN", raising=False)
    monkeypatch.setattr(context_recycle, "gate", lambda *args: None)
    monkeypatch.setattr(swarm_gates, "log", lambda message, payload: logged.append((message, payload)))

    assert swarm_gates.pretool("Bash", {"command": "ls"}, "s1") is None
    assert hosted == [("sw", "tasks/t1", claim_watch.READ_TIMEOUT)]
    assert claim_watch.READ_TIMEOUT == 2
    assert logged == [("claim watch failed", {"error": "timed out"})]


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
