import subprocess
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from scripts.agent_choice import ALL_FULL
from scripts.swarm import reaper
from scripts.swarm.pane import PaneObservation
from scripts.swarm.runtime import HerdrRuntime, herdr_target
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2.runtime.base import (
    BACKEND_VARIABLE,
    DISABLED_VARIABLE,
    LOCAL,
    Capability,
    Outcome,
    Recovery,
    RuntimeRouter,
    SpawnRequest,
    Status,
    foreign,
)
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from tests.swarm.profile_fixture import validated

REMOTE = "kubernetes"
PRIVATE = "private-target-marker"


class FakeRuntime:
    def __init__(self, backend, capabilities):
        self.backend, self.capabilities = backend, frozenset(capabilities)
        self.objects, self.calls = {}, []

    def spawn(self, request):
        self.calls.append(("spawn", request.name))
        agent = AgentRecord(
            request.name,
            request.lane,
            request.task["id"],
            pane_id=f"{self.backend}:{request.name}",
            runtime_backend=self.backend,
            runtime_target={"endpoint": PRIVATE} if self.backend == REMOTE else {},
        )
        self.objects.setdefault(request.name, agent)
        return Outcome("spawn", Status.OK, self.backend, self.objects[request.name])

    def observe(self, agent):
        self.calls.append(("observe", agent.name))
        state = "working" if agent.name in self.objects else "unknown"
        return Outcome("observe", Status.OK, self.backend, PaneObservation(state))

    def command(self, agent, text):
        self.calls.append(("command", agent.name))
        return Outcome("command", Status.OK, self.backend, "accepted")

    def drain(self, agent):
        self.calls.append(("drain", agent.name))
        return Outcome("drain", Status.OK, self.backend)

    def terminate(self, agent, homes=()):
        self.calls.append(("terminate", agent.name))
        self.objects.pop(agent.name, None)
        return Outcome("terminate", Status.OK, self.backend)

    def recover(self, agent, mode, config=None, text=""):
        self.calls.append((f"recover:{mode}", agent.name))
        return Outcome("recover", Status.OK, self.backend, self.objects.get(agent.name))


def local_fake():
    return FakeRuntime(LOCAL, set(Capability) - {Capability.DRAIN})


def remote_fake():
    return FakeRuntime(
        REMOTE,
        {Capability.SPAWN, Capability.OBSERVE, Capability.DRAIN, Capability.TERMINATE, Capability.RECOVER},
    )


def request(name="engineer@a1b2c3-0001", task="t1"):
    return SpawnRequest(SimpleNamespace(slug="sw"), "eng", name, {"id": task})


def pair(default=LOCAL, disabled=()):
    local, remote = local_fake(), remote_fake()
    return local, remote, RuntimeRouter([local, remote], default, disabled)


def config(tmp_path):
    return SimpleNamespace(slug="sw", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate")


def herdr_runtime(tmp_path, monkeypatch, panes=None, run=None):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    calls, panes = [], panes if panes is not None else {}

    def started(argv, **kwargs):
        out = "status=started\nroute_status=routed\npane_id=w1:p1\naccount=a1\n"
        return SimpleNamespace(returncode=0, stdout=validated(argv, out), stderr="")

    def herdr(args):
        calls.append(args)
        if args[:2] == ["agent", "get"]:
            if args[2] not in panes:
                raise RuntimeError("agent_not_found")
            return {"agent": panes[args[2]]}
        if args[:2] == ["pane", "read"]:
            return {"text": "ready"}
        return {}

    runtime = HerdrRuntime(home=tmp_path, run=run or started, herdr=herdr, choose=lambda *_: ("claude", "open"))
    runtime.end = lambda name, pid, homes: reaper.Outcome()
    return runtime, calls


# T-SV2-RUN-01-A


@pytest.mark.parametrize("fixture", ["first", "second"])
def test_the_local_swarm_spawns_observes_and_retires_through_the_protocol(tmp_path, monkeypatch, fixture):
    home = tmp_path / fixture
    pane = {"pane_id": "w1:p1", "name": "engineer@a1b2c3-0001", "agent_status": "working"}
    herdr, calls = herdr_runtime(home, monkeypatch, panes={"w1:p1": pane})
    remote = remote_fake()
    router = RuntimeRouter([LocalHerdrRuntime(herdr), remote])
    spawned = router.spawn(
        SpawnRequest(config(home), "eng", "engineer@a1b2c3-0001", {"id": "t1", "title": "x", "profile": "engineer"})
    )
    assert (spawned.status, spawned.backend, spawned.value.pane_id) == (Status.OK, LOCAL, "w1:p1")
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id=spawned.value.pane_id)
    assert router.observe(agent) == Outcome("observe", Status.OK, LOCAL, PaneObservation("working"))
    assert router.terminate(agent) == Outcome("terminate", Status.OK, LOCAL)
    assert ["pane", "close", "w1:p1"] in calls
    assert remote.calls == []
    assert router.capability_failures_total() == 0


def test_spawn_routes_to_the_default_backend_and_each_operation_reaches_only_its_owner():
    local, remote, router = pair(default=REMOTE)
    placed = router.spawn(request()).value
    assert placed.runtime_backend == REMOTE
    assert router.observe(placed).value == PaneObservation("working")
    assert router.drain(placed) == Outcome("drain", Status.OK, REMOTE)
    assert router.terminate(placed) == Outcome("terminate", Status.OK, REMOTE)
    assert local.calls == []
    assert remote.calls == [
        ("spawn", placed.name),
        ("observe", placed.name),
        ("drain", placed.name),
        ("terminate", placed.name),
    ]


def test_a_local_agent_is_commanded_and_resumed_on_local_alone():
    local, remote, router = pair()
    agent = router.spawn(request()).value
    assert router.command(agent, "wake") == Outcome("command", Status.OK, LOCAL, "accepted")
    assert router.recover(agent, Recovery.RESUME).status is Status.OK
    assert router.recover(agent).status is Status.OK
    assert local.calls == [
        ("spawn", agent.name),
        ("command", agent.name),
        ("recover:resume", agent.name),
        ("recover:reattach", agent.name),
    ]
    assert remote.calls == []


def test_an_outcome_is_ok_only_for_the_ok_status():
    assert Outcome("spawn", Status.OK, LOCAL).ok
    assert not any(Outcome("spawn", status, LOCAL).ok for status in Status if status is not Status.OK)


# T-SV2-RUN-01-B


def test_a_backend_without_native_resume_reports_unsupported_and_starts_nothing():
    local, remote, router = pair(default=REMOTE)
    agent = router.spawn(request()).value
    before = {name: asdict(row) for name, row in remote.objects.items()}
    outcome = router.recover(agent, Recovery.RESUME, text="you were restored")
    assert outcome == Outcome("recover", Status.UNSUPPORTED, REMOTE, detail="kubernetes lacks native_resume")
    assert {name: asdict(row) for name, row in remote.objects.items()} == before
    assert remote.calls == [("spawn", agent.name)]
    assert local.calls == []
    assert router.failures == {(REMOTE, "recover"): 1}
    assert router.capability_failures_total() == 1


def test_each_missing_capability_is_unsupported_and_counted_per_backend_and_operation():
    local, remote, router = pair(default=REMOTE)
    agent = router.spawn(request()).value
    assert router.command(agent, "wake").status is Status.UNSUPPORTED
    assert router.command(agent, "wake").status is Status.UNSUPPORTED
    assert router.drain(replace(agent, runtime_backend=LOCAL)).detail == "local lacks drain"
    assert router.failures == {(REMOTE, "command"): 2, (LOCAL, "drain"): 1}
    assert router.capability_failures_total() == 3
    assert local.calls == []


def test_a_spawn_needing_a_capability_the_backend_lacks_is_unsupported_and_launches_nothing():
    local, remote, router = pair(default=REMOTE)
    outcome = router.spawn(request(), needs=[Capability.NATIVE_RESUME, Capability.COMMAND])
    assert outcome == Outcome("spawn", Status.UNSUPPORTED, REMOTE, detail="kubernetes lacks native_resume, command")
    assert remote.calls == local.calls == []


def test_an_unsupported_outcome_names_no_runtime_target_value():
    _, _, router = pair(default=REMOTE)
    agent = router.spawn(request()).value
    assert PRIVATE not in repr(router.recover(agent, Recovery.RESUME))


def test_an_adapter_refuses_a_runtime_object_of_another_backend():
    herdr = SimpleNamespace(observe=pytest.fail, nudge=pytest.fail, retire=pytest.fail, recover=pytest.fail)
    adapter = LocalHerdrRuntime(herdr)
    remote = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", runtime_backend=REMOTE)
    refused = Outcome("observe", Status.REFUSED, LOCAL, detail="runtime object belongs to kubernetes")
    assert adapter.observe(remote) == refused
    assert adapter.command(remote, "wake") == replace(refused, operation="command")
    assert adapter.terminate(remote) == replace(refused, operation="terminate")
    assert adapter.recover(remote, Recovery.RESUME) == replace(refused, operation="recover")


def test_foreign_passes_an_object_of_the_same_backend():
    assert foreign(local_fake(), "observe", AgentRecord("a", "eng", "t")) is None


def test_an_unregistered_backend_object_is_unavailable_and_no_runtime_acts():
    local, remote, router = pair()
    stray = AgentRecord("engineer@a1b2c3-0002", "eng", "t2", runtime_backend="ssh")
    assert router.terminate(stray) == Outcome(
        "terminate", Status.UNAVAILABLE, "ssh", detail="no runtime registered for ssh"
    )
    assert local.calls == remote.calls == []
    assert router.capability_failures_total() == 0


# T-SV2-RUN-01-C


def test_disabling_the_remote_adapter_routes_new_spawns_local_and_keeps_its_records():
    local, remote, router = pair(default=REMOTE)
    attempt = router.spawn(request(task="t1")).value
    history = asdict(attempt)
    rolled = RuntimeRouter([local, remote], REMOTE, disabled=[REMOTE])
    assert rolled.spawn_backend() == LOCAL
    fresh = rolled.spawn(request("engineer@a1b2c3-0002", "t2")).value
    assert fresh.runtime_backend == LOCAL
    assert rolled.terminate(attempt) == Outcome(
        "terminate", Status.UNAVAILABLE, REMOTE, detail="runtime kubernetes is disabled"
    )
    assert asdict(attempt) == history
    assert attempt.name in remote.objects
    assert local.calls == [("spawn", fresh.name)]
    restored = RuntimeRouter([local, remote], REMOTE)
    assert restored.observe(attempt).value == PaneObservation("working")
    assert restored.spawn(request(task="t1")).value is remote.objects[attempt.name]
    assert len(remote.objects) == 1


def test_from_environ_reads_the_default_and_disabled_backends():
    local, remote = local_fake(), remote_fake()
    router = RuntimeRouter.from_environ(
        [local, remote], {BACKEND_VARIABLE: REMOTE, DISABLED_VARIABLE: f" {REMOTE} , ,other"}
    )
    assert (router.default, router.disabled) == (REMOTE, frozenset({REMOTE, "other"}))
    assert router.spawn_backend() == LOCAL
    assert RuntimeRouter.from_environ([local], {BACKEND_VARIABLE: ""}).default == LOCAL
    assert RuntimeRouter.from_environ([local], {}).disabled == frozenset()


def test_a_default_without_an_adapter_spawns_local():
    router = RuntimeRouter([local_fake(), remote_fake()], "ssh")
    assert router.spawn(request()).backend == LOCAL


def test_spawn_is_unavailable_when_local_itself_is_disabled_or_missing():
    _, remote, router = pair(default=REMOTE, disabled=[REMOTE, LOCAL])
    assert router.spawn(request()) == Outcome("spawn", Status.UNAVAILABLE, LOCAL, detail="no enabled runtime for local")
    assert RuntimeRouter([remote_fake()], LOCAL).spawn(request()).status is Status.UNAVAILABLE
    assert remote.calls == []


# Local herdr adapter


def test_a_timed_out_spawn_is_ambiguous(tmp_path, monkeypatch):
    def hang(argv, **kwargs):
        if "init-agent" in argv:
            raise subprocess.TimeoutExpired(argv, 300)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    herdr, _ = herdr_runtime(tmp_path, monkeypatch, run=hang)
    outcome = LocalHerdrRuntime(herdr).spawn(
        SpawnRequest(config(tmp_path), "eng", "engineer@x-1", {"id": "t1", "title": "x", "profile": "engineer"})
    )
    assert outcome == Outcome("spawn", Status.AMBIGUOUS, LOCAL, detail="init-agent timed out for engineer@x-1")


@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("unsupported resume: original profile is missing", Status.UNSUPPORTED),
        (ALL_FULL, Status.UNAVAILABLE),
        ("init-agent exit 1", Status.REFUSED),
    ],
)
def test_a_failed_spawn_maps_its_error_to_a_typed_status(message, status):
    def spawn(*args):
        raise SpawnError(message)

    outcome = LocalHerdrRuntime(SimpleNamespace(spawn=spawn)).spawn(request())
    assert outcome == Outcome("spawn", status, LOCAL, detail=message)


def test_a_timeout_cause_on_any_message_is_ambiguous():
    def spawn(*args):
        raise SpawnError("unsupported") from subprocess.TimeoutExpired("init-agent", 1)

    assert LocalHerdrRuntime(SimpleNamespace(spawn=spawn)).spawn(request()).status is Status.AMBIGUOUS


def test_an_unknown_observation_is_ambiguous_not_dead(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w1:p1")
    observed = LocalHerdrRuntime(herdr).observe(agent)
    assert observed == Outcome("observe", Status.AMBIGUOUS, LOCAL, PaneObservation("unknown"))


def test_command_delivers_through_the_herdr_nudge(tmp_path, monkeypatch):
    herdr, calls = herdr_runtime(tmp_path, monkeypatch)
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w1:p1")
    assert LocalHerdrRuntime(herdr).command(agent, "wake") == Outcome("command", Status.OK, LOCAL, "accepted")
    assert calls == [["agent", "prompt", "w1:p1", "[swarm delivery] wake"]]


def test_local_drain_is_unsupported():
    outcome = LocalHerdrRuntime(SimpleNamespace()).drain(AgentRecord("a", "eng", "t"))
    assert outcome == Outcome("drain", Status.UNSUPPORTED, LOCAL, detail="local herdr has no drain")


def test_a_refused_retirement_carries_the_herdr_refusal(tmp_path, monkeypatch):
    herdr, _ = herdr_runtime(tmp_path, monkeypatch)
    herdr.end = lambda name, pid, homes: reaper.Outcome(process=42, refusal="shared process group")
    agent = AgentRecord("engineer@a1b2c3-0001", "eng", "t1", pane_id="w1:p1")
    outcome = LocalHerdrRuntime(herdr).terminate(agent, homes=("/scratch",))
    refusal = {"process": 42, "refusal": "shared process group"}
    assert outcome == Outcome("terminate", Status.REFUSED, LOCAL, refusal, "shared process group")


def test_terminate_hands_the_scratch_homes_to_the_herdr_retire():
    seen = {}

    def retire(agent, homes=()):
        seen["homes"] = homes
        return True

    LocalHerdrRuntime(SimpleNamespace(retire=retire)).terminate(AgentRecord("a", "eng", "t"), homes=("/s",))
    assert seen == {"homes": ("/s",)}


def test_reattach_finds_the_live_pane_by_name_or_reports_it_unavailable(tmp_path, monkeypatch):
    pane = {"pane_id": "w1:p1", "agent": "claude"}
    herdr, _ = herdr_runtime(tmp_path, monkeypatch, panes={herdr_target("engineer@a1b2c3-0001"): pane})
    adapter = LocalHerdrRuntime(herdr)
    live = AgentRecord("engineer@a1b2c3-0001", "eng", "t1")
    gone = AgentRecord("engineer@a1b2c3-0002", "eng", "t2")
    assert adapter.recover(live, Recovery.REATTACH) == Outcome("recover", Status.OK, LOCAL, Placed("w1:p1", "claude"))
    assert adapter.recover(gone, Recovery.REATTACH) == Outcome("recover", Status.UNAVAILABLE, LOCAL, Placed("", ""))


def test_resume_reopens_through_the_herdr_resume_and_types_its_refusal():
    seen = []

    def resume(config, agent, text):
        seen.append((config, agent.name, text))
        if agent.task == "bad":
            raise SpawnError("unsupported resume: original profile is missing")
        return Placed("w1:p2", "claude")

    adapter = LocalHerdrRuntime(SimpleNamespace(resume=resume))
    good, bad = AgentRecord("a", "eng", "ok"), AgentRecord("b", "eng", "bad")
    assert adapter.recover(good, Recovery.RESUME, "cfg", "back") == Outcome(
        "recover", Status.OK, LOCAL, Placed("w1:p2", "claude")
    )
    assert adapter.recover(bad, Recovery.RESUME, "cfg", "back") == Outcome(
        "recover", Status.UNSUPPORTED, LOCAL, detail="unsupported resume: original profile is missing"
    )
    assert seen == [("cfg", "a", "back"), ("cfg", "b", "back")]


def test_the_local_adapter_declares_every_capability_but_drain_unlike_the_remote_fixture():
    assert LocalHerdrRuntime(None).capabilities == frozenset(Capability) - {Capability.DRAIN}
    assert remote_fake().capabilities != local_fake().capabilities
