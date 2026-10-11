import json
import subprocess

import pytest

from scripts.swarm.delivery import marked
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import NUDGE
from scripts.swarm_v2.herdr.capabilities import Incompatible, Unreachable
from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.target import BARE, Target
from scripts.swarm_v2.kubernetes import prompts as prompts_module
from scripts.swarm_v2.kubernetes.adapter import KubernetesRuntime
from scripts.swarm_v2.kubernetes.prompts import TerminalPrompts
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.runtime.base import LOCAL, Capability, Outcome, RuntimeRouter, Status
from scripts.swarm_v2.runtime.local import LocalHerdrRuntime
from scripts.swarm_v2.runtime.routed import RoutedRuntime
from tests import sv2_hdr03_cases, sv2_hdr04_cases
from tests.sv2_hdr04_cases import Client, Fleet

pytestmark = pytest.mark.unit
FX = sv2_hdr04_cases.fixture()
PANE = FX["pane_id"]
ENDPOINT_A, ENDPOINT_B = sv2_hdr03_cases.endpoints()
EXE_A, EXE_B = ENDPOINT_A.scope.execution_id, ENDPOINT_B.scope.execution_id
TEXT = NUDGE.format(slug="sw")


class Listing(Fleet):
    """The Anton herdr client that also answers agent list for each saved machine's server."""

    def __init__(self) -> None:
        super().__init__()
        self.unanswered: dict[str, subprocess.CompletedProcess | str] = {}

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        if command[3:5] != ["agent", "list"]:
            return super().__call__(command)
        self.commands.append(list(command))
        if command[2] in self.lost:
            raise subprocess.TimeoutExpired(command, 30)
        server = self._machine_server(command[2])
        if server is None:
            return subprocess.CompletedProcess(command, 1, "", "unknown machine")
        reply = {"id": "cli:agent:list", "result": {"agents": list(server["panes"].values()), "type": "agent_list"}}
        return subprocess.CompletedProcess(command, 0, json.dumps(reply), "")


def world(tmp_path):
    fleet = Listing()
    client = Client(fleet, tmp_path)
    client.register([ENDPOINT_A, ENDPOINT_B])
    ids = iter(f"op-{n}" for n in range(1, 100))
    return fleet, client, TerminalPrompts(client.router, lambda: next(ids))


def remote(execution_id=EXE_A, generation=ENDPOINT_A.scope.generation, **fields):
    return AgentRecord(
        "engineer@a1b2c3-0001",
        "eng",
        "t1",
        execution_id=execution_id,
        generation=generation,
        runtime_backend=BACKEND,
        **fields,
    )


def profile(client, execution_id):
    return client.router.store.load()[execution_id].profile_id


class Herdr:
    def __init__(self) -> None:
        self.nudged = []

    def nudge(self, agent, text):
        self.nudged.append((agent.name, text))


def tick_runtime(prompts):
    herdr = Herdr()
    kubernetes = KubernetesRuntime(lambda request: None, lambda request: {}, prompts)
    return herdr, RoutedRuntime(herdr, RuntimeRouter([LocalHerdrRuntime(herdr), kubernetes], LOCAL))


def test_an_idle_remote_agent_receives_the_tick_nudge_in_its_own_terminal_pane(tmp_path):
    fleet, client, prompts = world(tmp_path)
    herdr, runtime = tick_runtime(prompts)

    assert runtime.nudge(remote(), TEXT) is None

    assert fleet.received() == {"local": [], EXE_A: [marked(TEXT)], EXE_B: []}
    assert herdr.nudged == []
    machine = profile(client, EXE_A)
    assert fleet.mutations() == [["herdr", "--machine", machine, "agent", "prompt", PANE, marked(TEXT)]]


def test_an_agent_pane_with_a_letter_pane_id_receives_the_nudge(tmp_path):
    fleet, client, prompts = world(tmp_path)
    fleet.remote[EXE_A]["panes"] = {"w30:p7K": {"pane_id": "w30:p7K"}}

    assert prompts.command(remote(), "wake").ok

    machine = profile(client, EXE_A)
    assert fleet.mutations() == [["herdr", "--machine", machine, "agent", "prompt", "w30:p7K", marked("wake")]]
    assert fleet.received()[EXE_A] == [marked("wake")]


def test_a_command_lists_the_agents_of_its_own_server_before_it_prompts(tmp_path):
    fleet, client, prompts = world(tmp_path)

    outcome = prompts.command(remote(EXE_B, ENDPOINT_B.scope.generation), "wake")

    assert outcome == Outcome("command", Status.OK, BACKEND, "accepted")
    machine = profile(client, EXE_B)
    assert [command for command in fleet.commands if command[1] == "--machine" and command[3] == "agent"] == [
        ["herdr", "--machine", machine, "agent", "list"],
        ["herdr", "--machine", machine, "agent", "prompt", PANE, marked("wake")],
    ]
    assert fleet.received() == {"local": [], EXE_A: [], EXE_B: [marked("wake")]}


def test_an_agent_target_is_its_one_agent_pane_with_its_machine_label(tmp_path):
    _, client, _ = world(tmp_path)
    record = client.router.store.load()[EXE_B]

    found = prompts_module.agent_target(client.router, remote(EXE_B, ENDPOINT_B.scope.generation, conversation_id="c"))

    incarnation = f"{EXE_B}/{ENDPOINT_B.scope.generation}"
    assert found == (Target(record.profile_id, record.session, incarnation, PANE, "c"), ENDPOINT_B.name)


def test_each_nudge_takes_a_new_operation_id_so_a_later_idle_streak_is_typed_again(tmp_path):
    fleet, _, prompts = world(tmp_path)

    assert prompts.command(remote(), "first").ok and prompts.command(remote(), "second").ok

    assert fleet.received()[EXE_A] == [marked("first"), marked("second")]
    assert sorted(json.loads((tmp_path / "deliveries.json").read_text())["deliveries"]) == ["op-1", "op-2"]


def test_the_default_operation_id_is_new_for_every_prompt():
    first, second = prompts_module.operation_id(), prompts_module.operation_id()
    assert first != second
    assert first.startswith("prompt-") and len(first) == len("prompt-") + 32


def test_an_unreachable_terminal_is_refused_with_its_reason(tmp_path):
    fleet, client, prompts = world(tmp_path)
    fleet.lost.add(profile(client, EXE_A))

    outcome = prompts.command(remote(), "wake")

    incarnation = ENDPOINT_A.incarnation
    assert outcome == Outcome(
        "command", Status.REFUSED, BACKEND, detail=f"terminal unreachable: {incarnation} did not list its agents"
    )
    assert fleet.received()[EXE_A] == []


def test_a_herdr_client_that_cannot_start_is_unreachable(tmp_path, monkeypatch):
    fleet, _, prompts = world(tmp_path)
    answer = fleet.__call__

    def broken(command):
        if command[3:5] == ["agent", "list"]:
            raise OSError("herdr is not installed")
        return answer(command)

    monkeypatch.setattr(prompts.router, "run", broken)

    outcome = prompts.command(remote(), "wake")

    assert outcome == Outcome(
        "command",
        Status.REFUSED,
        BACKEND,
        detail=f"terminal unreachable: {ENDPOINT_A.incarnation} did not list its agents",
    )


def test_a_delivery_journal_that_cannot_be_written_is_ambiguous(tmp_path, monkeypatch):
    fleet, _, prompts = world(tmp_path)

    def unwritable(entries):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(prompts.router.deliveries, "save", unwritable)

    outcome = prompts.command(remote(), "wake")

    assert outcome == Outcome(
        "command", Status.AMBIGUOUS, BACKEND, detail="terminal journal: [Errno 13] Permission denied"
    )
    assert fleet.mutations() == []


def test_the_tick_reports_a_nudge_to_an_unreachable_terminal_as_refused_with_the_reason(tmp_path):
    fleet, client, prompts = world(tmp_path)
    fleet.lost.add(profile(client, EXE_A))
    _, runtime = tick_runtime(prompts)

    refused = runtime.nudge(remote(), TEXT)

    assert refused == f"terminal unreachable: {ENDPOINT_A.incarnation} did not list its agents"
    assert fleet.mutations() == []


def test_a_server_that_answers_agent_list_with_an_error_is_unreachable(tmp_path):
    fleet, client, prompts = world(tmp_path)
    fleet.hosts.pop(ENDPOINT_A.target)

    outcome = prompts.command(remote(), "wake")

    assert (outcome.status, outcome.detail) == (
        Status.REFUSED,
        f"terminal unreachable: {ENDPOINT_A.incarnation} did not list its agents",
    )


def test_an_execution_with_no_saved_machine_is_refused_before_any_herdr_command(tmp_path):
    fleet, _, prompts = world(tmp_path)
    before = list(fleet.commands)

    outcome = prompts.command(remote("exe-cccccccccccccccccccccccccccccccc", 1), "wake")

    assert outcome == Outcome(
        "command",
        Status.REFUSED,
        BACKEND,
        detail="terminal unregistered: exe-cccccccccccccccccccccccccccccccc has no saved herdr machine",
    )
    assert fleet.commands == before


@pytest.mark.parametrize("fields", [{"execution_id": ""}, {"generation": 0}])
def test_an_agent_without_an_execution_identity_is_refused_before_any_herdr_command(tmp_path, fields):
    fleet, _, prompts = world(tmp_path)
    before = list(fleet.commands)

    outcome = prompts.command(remote(**{"execution_id": EXE_A, "generation": 2, **fields}), "wake")

    assert outcome == Outcome("command", Status.REFUSED, BACKEND, detail="no execution identity")
    assert fleet.commands == before


def test_an_agent_of_another_generation_than_the_saved_machine_is_refused(tmp_path):
    fleet, _, prompts = world(tmp_path)

    outcome = prompts.command(remote(generation=ENDPOINT_A.scope.generation + 1), "wake")

    assert outcome.status is Status.REFUSED
    assert outcome.detail == f"terminal stale_incarnation: {EXE_A}/{ENDPOINT_A.scope.generation + 1} was replaced"
    assert fleet.mutations() == []


@pytest.mark.parametrize(
    ("panes", "count"),
    [({}, 0), ({PANE: {"pane_id": PANE}, "w2:p1": {"pane_id": "w2:p1"}}, 2)],
)
def test_a_server_without_exactly_one_agent_pane_is_refused(tmp_path, panes, count):
    fleet, _, prompts = world(tmp_path)
    fleet.remote[EXE_A]["panes"] = panes

    outcome = prompts.command(remote(), "wake")

    assert outcome == Outcome(
        "command",
        Status.REFUSED,
        BACKEND,
        detail=f"terminal agent_pane: {ENDPOINT_A.incarnation} lists {count} agent panes, not one",
    )
    assert fleet.mutations() == []


@pytest.mark.parametrize("stdout", ["not json", "[]", '{"result": []}', '{"result": {"agents": {}}}'])
def test_an_unreadable_agent_list_is_refused(tmp_path, monkeypatch, stdout):
    fleet, _, prompts = world(tmp_path)
    answer = fleet.__call__

    def listed(command):
        if command[3:5] == ["agent", "list"]:
            return subprocess.CompletedProcess(command, 0, stdout, "")
        return answer(command)

    monkeypatch.setattr(prompts.router, "run", listed)

    outcome = prompts.command(remote(), "wake")

    assert (outcome.status, outcome.detail) == (
        Status.REFUSED,
        f"terminal agent_list: {ENDPOINT_A.incarnation} answered an unreadable agent list",
    )


def test_an_agent_row_without_a_pane_id_is_not_an_agent_pane(tmp_path):
    fleet, _, prompts = world(tmp_path)
    fleet.remote[EXE_A]["panes"] = {PANE: {"pane_id": PANE}, "": {"name": "detached"}, "x": "garbage"}

    assert prompts.command(remote(), "wake").ok
    assert fleet.received()[EXE_A] == [marked("wake")]


def test_a_conversation_the_pane_no_longer_holds_is_refused(tmp_path):
    fleet, _, prompts = world(tmp_path)

    outcome = prompts.command(remote(conversation_id="99999999-9999-4999-8999-999999999999"), "wake")

    assert (outcome.status, outcome.detail) == (
        Status.REFUSED,
        f"terminal conversation_mismatch: {PANE} holds another conversation",
    )
    assert fleet.mutations() == []


def test_the_expected_conversation_is_confirmed_before_the_prompt(tmp_path):
    fleet, _, prompts = world(tmp_path)

    assert prompts.command(remote(conversation_id=FX["remote"][EXE_A]["conversation_id"]), "wake").ok
    assert fleet.received()[EXE_A] == [marked("wake")]


def test_a_prompt_whose_outcome_is_unknown_is_ambiguous(tmp_path, monkeypatch):
    fleet, _, prompts = world(tmp_path)
    answer = fleet.__call__

    def lost_prompt(command):
        if command[3:5] == ["agent", "prompt"]:
            raise subprocess.TimeoutExpired(command, 30)
        return answer(command)

    monkeypatch.setattr(prompts.router, "run", lost_prompt)

    outcome = prompts.command(remote(), "wake")

    assert outcome == Outcome(
        "command",
        Status.AMBIGUOUS,
        BACKEND,
        detail=f"terminal ambiguous_delivery: op-1 may have reached {ENDPOINT_A.incarnation}/{PANE}",
    )


def test_a_prompt_herdr_refuses_is_refused_with_the_exit(tmp_path, monkeypatch):
    fleet, _, prompts = world(tmp_path)
    answer = fleet.__call__

    def refused_prompt(command):
        if command[3:5] == ["agent", "prompt"]:
            return subprocess.CompletedProcess(command, 3, "", "agent not found")
        return answer(command)

    monkeypatch.setattr(prompts.router, "run", refused_prompt)

    outcome = prompts.command(remote(), "wake")

    assert (outcome.status, outcome.detail) == (
        Status.REFUSED,
        f"terminal send_refused: herdr exited 3 for op-1 on {ENDPOINT_A.incarnation}/{PANE}",
    )


def test_an_incompatible_server_is_refused_with_its_verdict(tmp_path, monkeypatch):
    fleet, client, prompts = world(tmp_path)

    def incompatible(target, incarnation, operation):
        raise Incompatible("herdr protocol is not 22")

    monkeypatch.setattr(client.qualifier, "require", incompatible)

    outcome = prompts.command(remote(), "wake")

    assert (outcome.status, outcome.detail) == (Status.REFUSED, "terminal incompatible: herdr protocol is not 22")
    assert fleet.mutations() == []


def test_a_server_whose_qualification_probe_cannot_connect_is_unreachable(tmp_path, monkeypatch):
    fleet, client, prompts = world(tmp_path)

    def unreachable(target, incarnation, operation):
        raise Unreachable("herdr --machine p status server: TimeoutExpired")

    monkeypatch.setattr(client.qualifier, "require", unreachable)

    outcome = prompts.command(remote(), "wake")

    assert outcome == Outcome(
        "command",
        Status.REFUSED,
        BACKEND,
        detail="terminal unreachable: herdr --machine p status server: TimeoutExpired",
    )


def test_the_agent_pane_listing_needs_a_server_qualified_target(tmp_path):
    _, client, _ = world(tmp_path)

    with pytest.raises(EndpointRefused) as refused:
        client.router.agent_panes(Target("", "default", ENDPOINT_A.incarnation, ""))

    assert (refused.value.reason, str(refused.value)) == ("bare_pane", BARE)


def test_the_agent_pane_listing_names_only_the_saved_machine_of_the_target(tmp_path):
    fleet, client, _ = world(tmp_path)
    record = client.router.store.load()[EXE_A]
    target = Target(record.profile_id, record.session, ENDPOINT_A.incarnation, "")

    assert client.router.agent_panes(target) == [PANE]
    assert fleet.commands[-1] == ["herdr", "--machine", record.profile_id, "agent", "list"]


def test_the_agent_listing_target_names_the_server_and_no_pane(tmp_path, monkeypatch):
    _, client, prompts = world(tmp_path)
    listed, agent_panes = [], client.router.agent_panes

    def recorded(target):
        listed.append(target)
        return agent_panes(target)

    monkeypatch.setattr(client.router, "agent_panes", recorded)

    assert prompts.command(remote(), "wake").ok
    assert listed == [Target(profile(client, EXE_A), "default", ENDPOINT_A.incarnation, "")]


def test_a_kubernetes_runtime_with_terminal_prompts_advertises_command(tmp_path):
    _, _, prompts = world(tmp_path)

    runtime = KubernetesRuntime(lambda request: None, lambda request: {}, prompts)

    assert runtime.capabilities == frozenset({Capability.SPAWN, Capability.COMMAND})
    assert KubernetesRuntime.capabilities == frozenset({Capability.SPAWN})


def test_a_kubernetes_runtime_without_terminal_prompts_reports_command_unsupported():
    herdr, runtime = tick_runtime(None)

    assert runtime.nudge(remote(), TEXT) == "kubernetes lacks command"
    assert KubernetesRuntime(lambda request: None, lambda request: {}).command(remote(), "wake") == Outcome(
        "command", Status.UNSUPPORTED, BACKEND, detail="kubernetes lacks command"
    )
    assert herdr.nudged == []
