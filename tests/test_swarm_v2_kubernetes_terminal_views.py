import json
import subprocess

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.herdr import visibility
from scripts.swarm_v2.herdr.capabilities import Unreachable
from scripts.swarm_v2.herdr.viewer import Viewer
from scripts.swarm_v2.herdr.visibility import CACHED, LIVE, UNVERIFIED, View
from scripts.swarm_v2.kubernetes import terminal_views
from scripts.swarm_v2.kubernetes.terminal_views import TerminalViews
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.operator_auth import OPERATOR
from tests import sv2_hdr03_cases, sv2_hdr04_cases, sv2_hdr05_cases
from tests.sv2_hdr05_cases import Clock, Workers

pytestmark = pytest.mark.unit
SLUG = "sw"
FX = sv2_hdr05_cases.fixture()
ROUTING = sv2_hdr04_cases.fixture()
PANE = ROUTING["pane_id"]
ENDPOINT_A, ENDPOINT_B = sv2_hdr03_cases.endpoints()
EXE_A, EXE_B = ENDPOINT_A.scope.execution_id, ENDPOINT_B.scope.execution_id


class Listing(Workers):
    """The worker herdr servers that also answer agent list, as the controller reads them to find each agent pane."""

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        if command[1:2] != ["--machine"] or command[3:5] != ["agent", "list"]:
            return super().__call__(command)
        self.commands.append(list(command))
        if command[2] in self.cut:
            raise subprocess.TimeoutExpired(command, 30)
        server = self._machine_server(command[2])
        if server is None:
            return subprocess.CompletedProcess(command, 1, "", "unknown machine")
        reply = {"id": "cli:agent:list", "result": {"agents": list(server["panes"].values()), "type": "agent_list"}}
        return subprocess.CompletedProcess(command, 0, json.dumps(reply), "")

    def verbs(self) -> list[tuple[str, str]]:
        return [tuple(command[3:5]) for command in self.commands if command[1:2] == ["--machine"]]


class Occupied(RedisStore):
    def __init__(self, redis) -> None:
        super().__init__(redis)
        self.occupants: list[AgentRecord] = []

    def execution_occupants(self, slug: str) -> dict[str, AgentRecord]:
        assert slug == SLUG
        return {agent.seat: agent for agent in self.occupants}


def remote(endpoint, seat, **fields):
    execution_id = endpoint.scope.execution_id
    defaults = {
        "seat": f"{seat}@{SLUG}",
        "execution_id": execution_id,
        "generation": endpoint.scope.generation,
        "runtime_backend": BACKEND,
        "conversation_id": ROUTING["remote"][execution_id]["conversation_id"],
    }
    return AgentRecord(FX["agents"][execution_id]["name"], "eng", seat, **(defaults | fields))


@pytest.fixture
def rig(tmp_path):
    import fakeredis

    clock = Clock(FX["start_ms"])
    workers = Listing(clock)
    client = sv2_hdr04_cases.Client(workers, tmp_path)
    client.register([ENDPOINT_A, ENDPOINT_B])
    store = Occupied(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 0, 0, state="paused"))
    store.occupants = [remote(ENDPOINT_A, "eng-1"), remote(ENDPOINT_B, "eng-2")]
    viewer = Viewer(client.router, workers, OPERATOR, clock, fresh_ms=FX["fresh_ms"])
    return workers, store, viewer, TerminalViews(store, SLUG, viewer)


def published(store, workers):
    return {
        agent: (row["state"], row["machine"], row["pane_id"], row["reason"])
        for agent, row in visibility.read(store, SLUG, workers.clock.now_ms).items()
    }


def machine(viewer, execution_id):
    return viewer.router.store.load()[execution_id].profile_id


def name(execution_id):
    return FX["agents"][execution_id]["name"]


def test_a_pass_publishes_each_remote_agent_live_on_its_own_machine_and_pane(rig):
    workers, store, _, views = rig

    returned = views()

    assert published(store, workers) == {
        name(EXE_A): (LIVE, ENDPOINT_A.name, PANE, ""),
        name(EXE_B): (LIVE, ENDPOINT_B.name, PANE, ""),
    }
    assert sorted(returned) == [name(EXE_A), name(EXE_B)]
    assert set(workers.verbs()) == {("status", "server"), ("agent", "list"), ("agent", "get"), ("pane", "read")}
    assert workers.launches() == []


def test_a_lost_ssh_path_caches_only_that_agent_and_never_drops_it(rig):
    workers, store, viewer, views = rig
    views()
    workers.cut.add(machine(viewer, EXE_B))

    views()

    assert published(store, workers) == {
        name(EXE_A): (LIVE, ENDPOINT_A.name, PANE, ""),
        name(EXE_B): (CACHED, ENDPOINT_B.name, PANE, "unreachable"),
    }
    assert workers.launches() == []


def test_an_agent_is_found_once_and_then_only_refreshed(rig):
    workers, _, _, views = rig

    views()
    views()

    assert workers.verbs().count(("agent", "list")) == 2


def test_a_new_conversation_on_the_same_attempt_is_found_again(rig):
    workers, store, viewer, views = rig
    views()
    store.occupants[0] = remote(ENDPOINT_A, "eng-1", conversation_id="replaced")

    views()

    assert workers.verbs().count(("agent", "list")) == 3
    assert viewer.watched[name(EXE_A)].target.conversation_id == "replaced"
    assert published(store, workers)[name(EXE_A)] == (UNVERIFIED, ENDPOINT_A.name, PANE, "conversation_mismatch")


def test_a_new_generation_whose_machine_is_not_registered_keeps_its_last_view(rig):
    workers, store, viewer, views = rig
    views()
    store.occupants[0] = remote(ENDPOINT_A, "eng-1", generation=ENDPOINT_A.scope.generation + 1)

    views()

    assert dict(viewer.router.mismatches) == {(machine(viewer, EXE_A), "stale_incarnation"): 1}
    assert viewer.watched[name(EXE_A)].target.incarnation == f"{EXE_A}/{ENDPOINT_A.scope.generation}"
    assert published(store, workers)[name(EXE_A)] == (LIVE, ENDPOINT_A.name, PANE, "")


def test_an_agent_that_left_the_swarm_is_no_longer_published(rig):
    workers, store, viewer, views = rig
    views()
    store.occupants = store.occupants[1:]

    views()

    assert sorted(viewer.watched) == [name(EXE_B)]
    assert sorted(published(store, workers)) == [name(EXE_B)]


def test_a_local_agent_is_not_published_and_an_unregistered_one_reads_unverified(rig):
    workers, store, viewer, views = rig
    local = AgentRecord("engineer@a1b2c3-0009", "eng", "t9", seat=f"eng-9@{SLUG}", conversation_id="c9")
    stray = AgentRecord(
        "engineer@a1b2c3-0008",
        "eng",
        "t8",
        seat=f"eng-8@{SLUG}",
        execution_id="exe-unregistered",
        generation=1,
        runtime_backend=BACKEND,
        conversation_id="c8",
    )
    store.occupants = [local, stray, remote(ENDPOINT_B, "eng-2")]
    viewer.fresh_ms = FX["fresh_ms"] + 5_000

    returned = views()

    assert sorted(viewer.watched) == [name(EXE_B)]
    assert published(store, workers) == {
        "engineer@a1b2c3-0008": (UNVERIFIED, "", "", "unregistered"),
        name(EXE_B): (LIVE, ENDPOINT_B.name, PANE, ""),
    }
    assert returned["engineer@a1b2c3-0008"] == View(
        "engineer@a1b2c3-0008", "", "", UNVERIFIED, reason="unregistered", fresh_ms=FX["fresh_ms"] + 5_000
    )


def test_an_agent_without_a_conversation_is_not_watched(rig):
    workers, store, viewer, views = rig
    store.occupants = [remote(ENDPOINT_A, "eng-1", conversation_id=""), remote(ENDPOINT_B, "eng-2")]

    views()

    assert sorted(viewer.watched) == [name(EXE_B)]
    assert published(store, workers)[name(EXE_A)] == (UNVERIFIED, ENDPOINT_A.name, "", "conversation_required")


def test_an_agent_whose_machine_is_cut_before_it_is_found_reads_unverified_and_is_found_later(rig):
    workers, store, viewer, views = rig
    workers.cut.add(machine(viewer, EXE_A))

    views()

    assert sorted(viewer.watched) == [name(EXE_B)]
    assert published(store, workers)[name(EXE_A)] == (UNVERIFIED, ENDPOINT_A.name, "", "unreachable")
    workers.cut.clear()

    views()

    assert published(store, workers)[name(EXE_A)] == (LIVE, ENDPOINT_A.name, PANE, "")


def test_an_unreachable_capability_probe_while_finding_a_pane_reads_unreachable(rig, monkeypatch):
    workers, store, _, views = rig

    def unreachable(router, agent):
        raise Unreachable(f"{agent.name}: TimeoutExpired")

    monkeypatch.setattr(terminal_views, "agent_target", unreachable)

    views()

    assert terminal_views.UNREACHABLE == "unreachable"
    assert published(store, workers) == {
        name(EXE_A): (UNVERIFIED, ENDPOINT_A.name, "", "unreachable"),
        name(EXE_B): (UNVERIFIED, ENDPOINT_B.name, "", "unreachable"),
    }


def test_a_views_pass_never_mutates_a_pane_or_an_authoritative_record(rig, tmp_path):
    workers, store, viewer, views = rig
    registrations = (tmp_path / "registrations.json").read_bytes()
    views()
    workers.cut.add(machine(viewer, EXE_B))
    store.occupants = [remote(ENDPOINT_A, "eng-1", conversation_id="replaced"), remote(ENDPOINT_B, "eng-2")]

    views()
    viewer.detach()
    views()

    assert workers.mutations() == [] and workers.launches() == []
    assert (tmp_path / "registrations.json").read_bytes() == registrations
    assert not (tmp_path / "deliveries.json").exists()


def test_aggregation_off_publishes_unverified_views_without_reading_any_pane(tmp_path):
    import fakeredis

    clock = Clock(FX["start_ms"])
    workers = Listing(clock)
    client = sv2_hdr04_cases.Client(workers, tmp_path)
    client.register([ENDPOINT_A, ENDPOINT_B])
    store = Occupied(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 0, 0, state="paused"))
    store.occupants = [remote(ENDPOINT_A, "eng-1")]
    viewer = Viewer(client.router, workers, OPERATOR, clock, fresh_ms=FX["fresh_ms"], aggregation=False)

    TerminalViews(store, SLUG, viewer)()

    assert published(store, workers) == {name(EXE_A): (UNVERIFIED, ENDPOINT_A.name, PANE, "never_live")}
    assert ("pane", "read") not in workers.verbs() and ("agent", "get") not in workers.verbs()
