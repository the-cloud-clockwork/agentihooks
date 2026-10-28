import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.hive import daemon, launch, orders
from scripts.swarm import command_runner, commands, prompt, resume
from scripts.swarm_ledger import ledger_workspace

pytestmark = pytest.mark.unit

TASK = {
    "id": "task",
    "title": "Carry the opening to the hive",
    "description": "launch this task",
    "workspace": "/controller/.agentihooks/swarm/fixture/tasks/task",
}


@pytest.fixture
def homes(tmp_path):
    controller, hive = tmp_path / "controller", tmp_path / "hive"
    controller.mkdir()
    hive.mkdir()
    return SimpleNamespace(controller=controller, hive=hive)


def at(home):
    return patch.object(Path, "home", return_value=home)


@pytest.fixture
def setup(homes):
    import fakeredis

    from scripts.hive.orders import Consumer, LaunchOrder
    from scripts.swarm import lease
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status

    redis = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(redis)
    config = SwarmConfig("fixture", "agentihooks", 1, 0)
    store.create(config)
    held = lease.acquire(store, "fixture", "controller")
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            seat="eng-1@fixture",
            state="launching",
            harness="claude",
            profile="engineer",
            account="account",
            hive="host",
            runtime_target={"host_id": "host"},
        ),
    )
    order = LaunchOrder(
        "fixture",
        agent.execution_id,
        agent.generation,
        "engineer",
        "eng",
        "task",
        "account",
        "claude",
        "subscription",
        "prompt",
        held.epoch,
        lease.now_ms(store) + 60000,
    )
    placed = Placed(
        "pane",
        "claude",
        "account",
        model="selected-model",
        effort="high",
        profile="engineer",
        profile_decision={"validation": {"pid": 12}},
    )
    seen = {}

    def spawn(request):
        seen["task"] = request.task
        seen["opening"] = prompt.build(
            request.config.slug, request.config.repo, request.lane, request.name, request.task
        )
        return Outcome("spawn", Status.OK, "local", placed)

    runtime = Mock()
    runtime.spawn.side_effect = spawn
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    return SimpleNamespace(
        redis=redis,
        store=store,
        config=config,
        held=held,
        agent=agent,
        order=order,
        runtime=runtime,
        seen=seen,
        consumer=Consumer(redis, "host", runtime),
    )


def carry(setup, homes):
    from scripts.swarm_v2.runtime.base import SpawnRequest

    with at(homes.controller):
        folder = ledger_workspace.scaffold("fixture", TASK)
        (folder / "progress.md").write_text("controller step\n", encoding="utf-8")
        request = SpawnRequest(setup.config, "eng", setup.agent.name, TASK)
        text = orders.build_opening(request)
        payload = orders.carried("fixture", TASK, text)
    setup.redis.set("prompt", json.dumps(payload))
    return text, payload


def test_the_hive_launches_with_the_identical_prompt_digest(setup, homes):
    text, payload = carry(setup, homes)
    with at(homes.hive):
        assert setup.consumer.apply(setup.order).status == "live"
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert payload[orders.DIGEST] == digest
    assert hashlib.sha256(setup.seen["opening"].encode()).hexdigest() == digest
    assert "~/.agentihooks/swarm/fixture/tasks/task" in text
    assert "/controller" not in text


def test_the_hive_materializes_the_seeds_below_its_own_home(setup, homes):
    carry(setup, homes)
    with at(homes.hive):
        setup.consumer.apply(setup.order)
    folder = homes.hive / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task"
    assert setup.seen["task"]["workspace"] == str(folder)
    assert (folder / "progress.md").read_text(encoding="utf-8") == "controller step\n"
    assert (folder / "steering.md").read_text(encoding="utf-8").startswith("# task: Carry the opening to the hive")
    assert (folder / "proof.md").read_text(encoding="utf-8") == ""


def test_the_hive_keeps_its_own_progress_on_relaunch(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"progress.md": "seed\n", "steering.md": "old\n"})
        (folder / "progress.md").write_text("seed\nhive step\n", encoding="utf-8")
        ledger_workspace.materialize("fixture", "task", {"progress.md": "seed\n", "steering.md": "new\n"})
    assert (folder / "progress.md").read_text(encoding="utf-8") == "seed\nhive step\n"
    assert (folder / "steering.md").read_text(encoding="utf-8") == "new\n"


def test_a_hive_holding_an_older_folder_takes_the_newer_copy(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"progress.md": "old step\n", "measure.sh": "v1\n"})
        sent = {"progress.md": "old step\nnewer step\n", "measure.sh": "v2\n"}
        ledger_workspace.materialize("fixture", "task", sent)
    assert (folder / "progress.md").read_text(encoding="utf-8") == "old step\nnewer step\n"
    assert (folder / "measure.sh").read_text(encoding="utf-8") == "v2\n"


@pytest.mark.parametrize("change", ["opening", "digest", "no opening", "no digest"])
def test_a_changed_opening_is_refused_before_launch(setup, homes, change):
    _, payload = carry(setup, homes)
    if change == "opening":
        payload[orders.OPENING] += " and something else"
    elif change == "digest":
        payload[orders.DIGEST] = "0" * 64
    else:
        del payload[orders.OPENING if change == "no opening" else orders.DIGEST]
    setup.redis.set("prompt", json.dumps(payload))
    with at(homes.hive):
        ack = setup.consumer.apply(setup.order)
    assert (ack.status, ack.target, ack.failure) == ("failed", {}, "prompt")
    setup.runtime.spawn.assert_not_called()


def test_hive_progress_lines_reach_the_ledger(setup, homes):
    carry(setup, homes)
    with at(homes.hive):
        setup.consumer.apply(setup.order)
        folder = ledger_workspace.folder("fixture", "task")
        with (folder / "progress.md").open("a", encoding="utf-8") as fh:
            fh.write("hive step one\nhive step two\n")
        setup.consumer.sync_workspaces(45)
    assert 0 < setup.redis.ttl(ledger_workspace.synced_key("host")) <= 45
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}, {"id": "other"}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        tails = ledger_workspace.published(setup.store, "fixture", ledger)
        with patch.object(command_runner, "status_report", return_value={}):
            command_runner.publish(setup.store, "fixture", ledger)
    assert tails == {"task": {"latest_progress": "controller step\nhive step one\nhive step two"}}
    assert commands.workspaces(setup.store, "fixture") == tails


def test_a_finished_remote_task_keeps_its_synced_lines(setup, homes):
    from dataclasses import replace

    carry(setup, homes)
    with at(homes.hive):
        setup.consumer.apply(setup.order)
        with (ledger_workspace.folder("fixture", "task") / "proof.md").open("a", encoding="utf-8") as fh:
            fh.write("hive proof\n")
        agent = setup.store.execution_registry.get("fixture", setup.agent.execution_id)
        setup.store.execution_registry.update("fixture", replace(agent, state="finished"))
        setup.consumer.sync_workspaces(45)
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        (ledger_workspace.folder("fixture", "task") / "proof.md").write_text("controller only\n", encoding="utf-8")
        tails = ledger_workspace.published(setup.store, "fixture", ledger)
    assert tails == {"task": {"latest_progress": "controller step", "latest_proof": "hive proof"}}


def test_a_hive_task_folder_reaches_the_controller(setup, homes):
    carry(setup, homes)
    with at(homes.hive):
        setup.consumer.apply(setup.order)
        folder = ledger_workspace.folder("fixture", "task")
        with (folder / "progress.md").open("a", encoding="utf-8") as fh:
            fh.write("hive step\n")
        (folder / "proof.md").write_text("hive proof\n", encoding="utf-8")
        (folder / "measure.sh").write_text("echo measured\n", encoding="utf-8")
        setup.consumer.sync_workspaces(45)
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        with patch.object(command_runner, "status_report", return_value={}):
            command_runner.publish(setup.store, "fixture", ledger)
    folder = homes.controller / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task"
    assert (folder / "progress.md").read_text(encoding="utf-8") == "controller step\nhive step\n"
    assert (folder / "proof.md").read_text(encoding="utf-8") == "hive proof\n"
    assert (folder / "measure.sh").read_text(encoding="utf-8") == "echo measured\n"
    assert (folder / "steering.md").read_text(encoding="utf-8").startswith("# task: Carry the opening to the hive")


def test_a_dead_hive_folder_still_reaches_the_controller(setup, homes):
    carry(setup, homes)
    with at(homes.hive):
        setup.consumer.apply(setup.order)
        (ledger_workspace.folder("fixture", "task") / "handoff.md").write_text("handoff\n", encoding="utf-8")
        setup.consumer.sync_workspaces(45)
    setup.redis.delete(ledger_workspace.synced_key("host"))
    assert setup.redis.ttl(ledger_workspace.files_key("host")) > 45
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        ledger_workspace.mirror(setup.store, "fixture", ledger)
    folder = homes.controller / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task"
    assert (folder / "handoff.md").read_text(encoding="utf-8") == "handoff\n"


def test_a_task_run_here_keeps_its_own_folder(setup, homes):
    carry(setup, homes)
    setup.redis.set(
        ledger_workspace.files_key("host"), json.dumps({"fixture": {"task": {"progress.md": "stale hive\n"}}})
    )
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="host"):
        ledger_workspace.mirror(setup.store, "fixture", ledger)
        assert (ledger_workspace.folder("fixture", "task") / "progress.md").read_text(encoding="utf-8") == (
            "controller step\n"
        )


def test_a_successor_hive_receives_the_files_its_predecessor_added(homes):
    with at(homes.controller):
        folder = ledger_workspace.scaffold("fixture", TASK)
        (folder / "mutation-fix.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
        payload = orders.carried("fixture", TASK, "text")
    with at(homes.hive):
        ledger_workspace.materialize("fixture", "task", payload[orders.SEEDS])
    folder = homes.hive / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task"
    assert (folder / "mutation-fix.patch").read_text(encoding="utf-8") == "--- a\n+++ b\n"


def test_the_folder_copy_holds_only_plain_text_files(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"progress.md": "step\n", "../escape": "x"})
        (folder / "nested").mkdir()
        (folder / "nested" / "inner.txt").write_text("inner", encoding="utf-8")
        (folder / "blob.bin").write_bytes(b"\xff\xfe\x00")
        (folder / "huge.log").write_text("x" * (ledger_workspace.SEED_CHARS + 1), encoding="utf-8")
        (folder / ".hidden").write_text("hidden", encoding="utf-8")
        (folder / "linked").symlink_to(homes.controller)
        assert ledger_workspace.seeds("fixture", "task") == {"progress.md": "step\n"}
    assert not (folder.parent / "escape").exists()


def test_the_file_cap_counts_only_added_files_that_travel(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"progress.md": "step\n", "proof.md": "proof\n"})
        (folder / "a0.log").write_text("x" * (ledger_workspace.SEED_CHARS + 1), encoding="utf-8")
        for index in range(1, ledger_workspace.FILES + 2):
            (folder / f"a{index:02}.txt").write_text(f"{index}\n", encoding="utf-8")
        sent = ledger_workspace.seeds("fixture", "task")
    assert sent["progress.md"] == "step\n" and sent["proof.md"] == "proof\n"
    assert sorted(set(sent) - {"progress.md", "proof.md"}) == [
        f"a{index:02}.txt" for index in range(1, ledger_workspace.FILES + 1)
    ]


def test_an_added_file_at_the_size_limit_still_travels(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"progress.md": "step\n"})
        (folder / "edge.log").write_text("x" * ledger_workspace.SEED_CHARS, encoding="utf-8")
        assert len(ledger_workspace.seeds("fixture", "task")["edge.log"]) == ledger_workspace.SEED_CHARS


def test_an_unreadable_or_linked_file_never_stops_the_files_after_it(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"progress.md": "step\n"})
        (folder / "a.bin").write_bytes(b"\xff\xfe\x00")
        (homes.controller / "outside.txt").write_text("outside\n", encoding="utf-8")
        (folder / "b.txt").symlink_to(homes.controller / "outside.txt")
        (folder / "c.txt").write_text("kept\n", encoding="utf-8")
        assert ledger_workspace.seeds("fixture", "task") == {"progress.md": "step\n", "c.txt": "kept\n"}


def test_each_hive_keeps_its_own_folder_files_key():
    assert ledger_workspace.files_key("one") != ledger_workspace.files_key("two")
    assert ledger_workspace.files_key("one").startswith(ledger_workspace.synced_key("one"))


def test_the_controller_steering_replaces_a_hive_steering_that_only_contains_it(homes):
    with at(homes.hive):
        folder = ledger_workspace.materialize("fixture", "task", {"steering.md": "new\nhive extra\n"})
        ledger_workspace.materialize("fixture", "task", {"steering.md": "new\n"})
    assert (folder / "steering.md").read_text(encoding="utf-8") == "new\n"


def test_the_mirror_fills_every_hive_task_into_a_fresh_home(homes):
    copies = {
        "first": {},
        "second": {"progress.md": "hive step\n", "steering.md": "hive steering\n", "../escape": "x"},
    }
    with at(homes.controller), patch.object(ledger_workspace, "_synced", return_value=copies):
        ledger_workspace.mirror(None, "fixture", {})
    folder = homes.controller / ".agentihooks" / "swarm" / "fixture" / "tasks"
    assert (folder / "second" / "progress.md").read_text(encoding="utf-8") == "hive step\n"
    assert sorted(path.name for path in folder.iterdir()) == ["second"]
    assert sorted(path.name for path in (folder / "second").iterdir()) == ["progress.md"]


def test_the_mirror_writes_utf8_under_an_ascii_locale(homes):
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from unittest.mock import patch\n"
        "from scripts.swarm_ledger import ledger_workspace\n"
        "copies = {'task': {'progress.md': 'caf\\u00e9\\n'}}\n"
        "with patch.object(Path, 'home', return_value=Path(sys.argv[1])):\n"
        "    with patch.object(ledger_workspace, '_synced', return_value=copies):\n"
        "        ledger_workspace.mirror(None, 'fixture', {})\n"
    )
    ascii_locale = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    done = subprocess.run(
        [sys.executable, "-c", script, str(homes.controller)],
        env={**os.environ, **ascii_locale},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    progress = homes.controller / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task" / "progress.md"
    assert progress.read_bytes() == "café\n".encode()


def test_the_mirror_names_utf8_when_it_writes(homes, monkeypatch):
    import codecs

    used = []
    real = Path.write_text

    def spy(self, text, *args, **kwargs):
        used.append(kwargs.get("encoding"))
        return real(self, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy)
    copies = {"task": {"progress.md": "step —\n"}}
    with at(homes.controller), patch.object(ledger_workspace, "_synced", return_value=copies):
        ledger_workspace.mirror(None, "fixture", {})
    assert len(used) == 1
    assert used[0] and codecs.lookup(used[0]).name == "utf-8"


def test_the_latest_execution_of_a_task_wins(setup):
    from dataclasses import replace

    older = replace(setup.agent, name="older", state="finished", started_at=1, runtime_target={"host_id": "a"})
    newer = replace(setup.agent, name="newer", state="ended", started_at=2, runtime_target={"host_id": "b"})
    live = replace(setup.agent, name="live", state="working", started_at=0)
    assert ledger_workspace.latest([newer, older])["task"].name == "newer"
    assert ledger_workspace.latest([live, newer, older])["task"].name == "live"


def test_seeds_carry_only_the_latest_text(homes):
    with at(homes.controller):
        folder = ledger_workspace.scaffold("fixture", TASK)
        (folder / "progress.md").write_text("x" * ledger_workspace.SEED_CHARS + "last\n", encoding="utf-8")
        (folder / "steering.md").write_text("# task\n" + "y" * ledger_workspace.SEED_CHARS, encoding="utf-8")
        sent = ledger_workspace.seeds("fixture", "task")
    assert len(sent["progress.md"]) == ledger_workspace.SEED_CHARS
    assert sent["progress.md"].endswith("last\n")
    assert sent["steering.md"] == "# task\n" + "y" * ledger_workspace.SEED_CHARS


def test_work_folder_seeds_are_read_and_written_as_utf8(homes, monkeypatch):
    import codecs

    used = []

    def spy(method):
        real = getattr(Path, method)

        def call(self, *args, **kwargs):
            if self.name in ledger_workspace.SEEDS:
                used.append((method, kwargs.get("encoding")))
            return real(self, *args, **kwargs)

        return call

    for method in ("open", "read_text", "write_text"):
        monkeypatch.setattr(Path, method, spy(method))
    with at(homes.hive):
        ledger_workspace.materialize("fixture", "task", {"steering.md": "steer —\n", "progress.md": "step —\n"})
        sent = ledger_workspace.seeds("fixture", "task")
    assert sent == {"steering.md": "steer —\n", "progress.md": "step —\n"}
    assert {method for method, _ in used} == {"open", "read_text", "write_text"}
    assert all(encoding and codecs.lookup(encoding).name == "utf-8" for _, encoding in used)


def test_published_tails_cover_every_task_in_the_ledger(setup, homes, monkeypatch):
    from dataclasses import replace

    agents = [
        replace(setup.agent, task="here", runtime_target={"host_id": "controller"}),
        replace(setup.agent, task="far", runtime_target={"host_id": "unsynced"}),
        replace(setup.agent, task="near", runtime_target={"host_id": "elsewhere"}),
    ]
    monkeypatch.setattr(setup.store, "agents", lambda slug: agents)
    setup.redis.set(ledger_workspace.synced_key("elsewhere"), json.dumps({"other": {"near": {"latest_proof": "x"}}}))
    ledger = {"tasks": [{"id": "bare"}, *({"id": task, "workspace": "w"} for task in ("here", "far", "near"))]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        ledger_workspace.materialize("fixture", "here", {"progress.md": "here step\n"})
        assert ledger_workspace.published(setup.store, "fixture", ledger) == {
            "here": {"latest_progress": "here step"},
            "far": {},
            "near": {},
        }
        assert ledger_workspace.published(setup.store, "fixture", {}) == {}


def test_a_local_task_reads_its_own_folder(setup, homes):
    carry(setup, homes)
    ledger = {"tasks": [{"id": "task", "workspace": TASK["workspace"]}]}
    with at(homes.controller), patch.object(commands, "hive_id", return_value="host"):
        tails = ledger_workspace.published(setup.store, "fixture", ledger)
    assert tails == {"task": {"latest_progress": "controller step"}}


def test_the_daemon_syncs_the_workspaces_it_hosts(setup, homes, monkeypatch):
    monkeypatch.setattr(daemon, "fence", lambda *args: False)
    monkeypatch.setattr(daemon.account_sessions, "local_processes", lambda: {})
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": {}, "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon, "execution_report", lambda *args: {"names": [], "panes": {}, "executions": {}})
    monkeypatch.setattr(orders.Consumer, "local", classmethod(lambda cls, redis, hive: setup.consumer))
    with at(homes.hive):
        ledger_workspace.materialize("fixture", "task", {"progress.md": "hive step\n"})
        daemon.beat(setup.redis, "host")
    synced = json.loads(setup.redis.get(ledger_workspace.synced_key("host")))
    assert synced == {"fixture": {"task": {"latest_progress": "hive step"}}}
    assert 0 < setup.redis.ttl(ledger_workspace.synced_key("host")) <= daemon.TTL_SECONDS


def test_the_synced_copy_sits_below_its_own_hive_key():
    from scripts.hive import registry

    assert ledger_workspace.synced_key("host") == f"{registry.key('host')}:workspaces"


def test_a_hive_syncs_only_the_worker_tasks_it_hosts(setup, homes, monkeypatch):
    from dataclasses import replace

    agents = [
        replace(setup.agent, task="mine"),
        replace(setup.agent, task="master", lane="master"),
        replace(setup.agent, task="elsewhere", runtime_target={"host_id": "other"}),
        replace(setup.agent, task="unlaunched", execution_id=""),
    ]
    monkeypatch.setattr(setup.consumer.store, "agents", lambda slug: agents)
    monkeypatch.setattr(setup.consumer.store, "slugs", lambda: ["fixture"])
    with at(homes.hive):
        for task in ("mine", "master", "elsewhere", "unlaunched"):
            ledger_workspace.materialize("fixture", task, {"progress.md": f"{task} step\n"})
        setup.consumer.sync_workspaces(45)
    synced = json.loads(setup.redis.get(ledger_workspace.synced_key("host")))
    assert synced == {"fixture": {"mine": {"latest_progress": "mine step"}}}


def test_the_opening_is_built_for_the_lane_on_the_controller(setup, homes, monkeypatch):
    from dataclasses import replace

    from scripts.swarm.store import MANUAL
    from scripts.swarm_v2.runtime.base import SpawnRequest

    calls = []
    monkeypatch.setattr(orders.prompt, "build", lambda *args, **kwargs: calls.append((args, kwargs)) or "text")
    config = replace(setup.config, lanes={"eng": {"role": "a hive engineer"}}, autonomy=MANUAL)
    assert orders.build_opening(SpawnRequest(config, "eng", "worker", TASK)) == "text"
    task = {**TASK, "harness": "claude", "workspace": "~/.agentihooks/swarm/fixture/tasks/task"}
    assert calls == [
        (("fixture", "agentihooks", "eng", "worker", task), {"role": "a hive engineer", "autonomy": MANUAL})
    ]
    orders.build_opening(SpawnRequest(config, "ci", "worker", {"id": "task"}))
    assert calls[1] == (
        ("fixture", "agentihooks", "ci", "worker", {"id": "task", "harness": "claude"}),
        {"role": None, "autonomy": MANUAL},
    )


def test_the_controller_carries_the_opening_by_redis_key(setup, homes):
    from scripts.hive import registry
    from scripts.swarm.capacity import Account
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import AgentRecord
    from scripts.swarm_v2.runtime.base import SpawnRequest

    hive = registry._default("host")
    hive.update(
        slots=[{"harness": "claude", "account": "account", "kind": "subscription"}],
        repos=[setup.config.repo],
        heartbeat_at=orders.lease.now_ms(setup.store),
        max_agents=3,
    )
    setup.redis.hset(registry.key("host"), mapping={k: registry._encode(k, v) for k, v in hive.items()})
    setup.redis.sadd(registry.INDEX, "host")
    from tests.hive.test_controller_launch import publish_room

    publish_room(setup.store, "host", 3)
    herdr = Mock(spec=HerdrRuntime)
    herdr._profile_decision.return_value = ProfileDecision("engineer", "task", "engineer")
    herdr._quota_accounts = [Account("claude", "account", "room", 0, 80, 80, 6)]
    herdr.planned_account.return_value = None
    launcher = launch.HiveLaunch(setup.store, "fixture", setup.held, herdr)
    launcher.opening = lambda request: f"opening for {request.name}"
    launcher.sleep = Mock(side_effect=StopIteration)
    name = setup.store.next_name("fixture", "eng")
    setup.store.put_agent("fixture", AgentRecord(name, "eng", "task", seat="eng-2@fixture", state="starting"))
    setup.store.seats.occupy("eng-2@fixture", name, 0)
    task = {**TASK, "seat": "eng-2@fixture", "controller_epoch": setup.held.epoch}
    with at(homes.controller), pytest.raises(StopIteration):
        launcher.spawn(SpawnRequest(setup.config, "eng", name, task))
    payload = json.loads(setup.redis.get(launcher.orders_for("task").prompt_ref))
    assert payload[orders.OPENING] == f"opening for {name}"
    assert payload[orders.DIGEST] == hashlib.sha256(f"opening for {name}".encode()).hexdigest()


def test_resume_texts_name_the_remote_folder_below_its_home(setup, homes):
    with at(homes.controller), patch.object(commands, "hive_id", return_value="controller"):
        restored = resume.restored_text("fixture", setup.agent, "ledger show")
        recovered = resume._recovery_text("fixture", setup.agent, TASK)
    assert "~/.agentihooks/swarm/fixture/tasks/task" in restored
    assert "~/.agentihooks/swarm/fixture/tasks/task" in recovered
    assert "/controller" not in restored + recovered


def test_resume_texts_keep_the_local_folder(setup, homes):
    with at(homes.controller), patch.object(commands, "hive_id", return_value="host"):
        restored = resume.restored_text("fixture", setup.agent, "ledger show")
        recovered = resume._recovery_text("fixture", setup.agent, TASK)
    assert str(homes.controller / ".agentihooks" / "swarm" / "fixture" / "tasks" / "task") in restored
    assert TASK["workspace"] in recovered


def test_a_local_recovery_without_a_recorded_folder_names_its_own(setup, homes):
    with at(homes.controller), patch.object(commands, "hive_id", return_value="host"):
        recovered = resume._recovery_text("fixture", setup.agent, {"id": "task"})
    assert f"task folder {homes.controller / '.agentihooks' / 'swarm' / 'fixture' / 'tasks' / 'task'} (" in recovered
