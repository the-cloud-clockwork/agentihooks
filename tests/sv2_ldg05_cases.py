import hashlib
import json
from pathlib import Path

import pytest

from scripts.swarm_v2.api.commands import CommandQueue, CommandsAPI, worker_command_ack_lag_seconds
from scripts.swarm_v2.worker.control import RouteTransport, WorkerControl
from tests import sv2_ldg02_cases

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/worker-commands.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SLUG = sv2_ldg02_cases.SLUG


class Network:
    """Fault injection between a worker and the API: drop requests, lose responses, repeat a poll response."""

    def __init__(self, world, token):
        self.world, self.token = world, token
        self.drop, self.lose = set(), set()
        self.repeat = None
        self.calls = []

    def send(self, method, path, body):
        endpoint = path.rsplit("/", 1)[-1]
        if endpoint in self.drop:
            raise ConnectionError("fixture request was dropped")
        if self.repeat is not None and endpoint == "commands":
            return self.repeat
        reply = self.world.commands_api.route(method, path, f"Bearer {self.token}", body)
        delivered = len(reply[1]["commands"]) if endpoint == "commands" and reply[0] == 200 else None
        self.calls.append([method, endpoint, reply[0], delivered])
        if endpoint in self.lose:
            raise ConnectionError("fixture response was lost")
        return reply


class World(sv2_ldg02_cases.World):
    def __init__(self, monkeypatch, folder: Path):
        super().__init__(monkeypatch)
        self.folder = folder
        self.queue = CommandQueue(self.store, SLUG)
        self.commands_api = self.restarted()
        self.ran = []

    def restarted(self):
        return CommandsAPI(self.grants, self.queue, INPUTS["poll_interval_ms"], INPUTS["poll_limit"])

    def worker(self, seat=sv2_ldg02_cases.INPUTS["seat"], task=sv2_ldg02_cases.INPUTS["task"], previous=""):
        agent, token = self.start(seat, task, previous)
        assert self.register(agent, token)[0] == 200
        network = Network(self, token)
        return agent, network, self.control(agent, network)

    def control(self, agent, network, name="worker"):
        handlers = {kind: self.handler(kind) for kind in ("answer", "cancel", "stop")}
        transport = RouteTransport(agent.execution_id, network.send)
        return WorkerControl(transport, self.folder / f"{agent.execution_id}-{name}.json", handlers)

    def handler(self, kind):
        def run(payload):
            self.ran.append([kind, payload])
            return {"status": "succeeded"}

        return run

    def issue(self, agent, kind, key, payload=None):
        return self.queue.issue(agent.execution_id, agent.generation, kind, payload or {}, key, INPUTS["expires_in_ms"])

    def later(self, ms):
        self.clock[0] += ms

    def state(self, agent, command):
        return self.queue.outcome(agent.execution_id, command["command_id"])["state"]

    def protected(self):
        return {key: value for key, value in super().protected().items() if not key.endswith("polls")}


def refusal(reply):
    return [reply[0], reply[1]["error_class"], reply[1]["retry"]]


def raised(action):
    from scripts.swarm_v2.auth_context import GrantRefused

    try:
        return ["returned", action()]
    except GrantRefused as error:
        return [error.error_class, str(error)]


def poll(world, agent, network):
    return world.commands_api.route(
        "GET", f"/v2/executions/{agent.execution_id}/commands", f"Bearer {network.token}", None
    )


def _positive(world):
    agent, network, control = world.worker()
    drain = world.issue(agent, "drain", "drain-1")
    world.later(INPUTS["delivery_delay_ms"])
    network.drop = {"ack"}
    control.step()
    unreachable = [control.may_mutate(), world.state(agent, drain)]
    network.drop = set()
    world.later(INPUTS["poll_interval_ms"])
    duplicated = network.repeat = poll(world, agent, network)
    control.step()
    control.step()
    network.repeat = None
    world.later(INPUTS["poll_interval_ms"])
    control.step()
    accepted = world.queue.outcome(agent.execution_id, drain["command_id"])
    world.later(INPUTS["poll_interval_ms"])
    control.checkpointed(INPUTS["checkpoint"])
    completed = world.queue.outcome(agent.execution_id, drain["command_id"])
    return {
        "issued": drain["state"],
        "while_ack_unreachable": unreachable,
        "duplicate_poll": [duplicated[0], [command["state"] for command in duplicated[1]["commands"]]],
        "calls": network.calls,
        "accepted": [accepted["state"], accepted["outcome"], control.may_mutate()],
        "completed": [completed["state"], completed["outcome"], control.may_mutate()],
        "accepted_before_completed": accepted["accepted_at_ms"] < completed["completed_at_ms"],
        "worker_command_ack_lag_seconds": worker_command_ack_lag_seconds(world.store, SLUG),
    }


def _rejection(world):
    agent, network, control = world.worker()
    answer = world.issue(agent, "answer", "answer-1", {"text": INPUTS["answer"]})
    stop = world.issue(agent, "stop", "stop-1")
    world.later(INPUTS["late_delivery_ms"])
    before = world.protected()
    control.step()
    late = world.commands_api.route(
        "POST",
        f"/v2/executions/{agent.execution_id}/commands/{answer['command_id']}/ack",
        f"Bearer {network.token}",
        {"payload_digest": answer["payload_digest"]},
    )
    assert world.protected() == before
    states = [world.state(agent, answer), world.state(agent, stop)]
    world.later(sv2_ldg02_cases.INPUTS["lease_ms"])
    successor, successor_network, successor_control = world.worker(previous=agent.execution_id)
    successor_control.step()
    return {
        "late_delivery": [network.calls, control.may_mutate()],
        "late_ack": refusal(late),
        "states": states,
        "successor_delivered": successor_network.calls,
        "old_conversation_poll": refusal(poll(world, agent, network)),
        "reissue_to_superseded": raised(lambda: world.issue(agent, "answer", "answer-2", {"text": INPUTS["answer"]})),
        "handlers_ran": world.ran,
        "worker_command_ack_lag_seconds": worker_command_ack_lag_seconds(world.store, SLUG),
    }


def _recovery(world):
    agent, network, control = world.worker()
    answer = world.issue(agent, "answer", "answer-1", {"text": INPUTS["answer"]})
    network.drop = {"commands", "ack", "complete"}
    control.step()
    world.commands_api = world.restarted()
    network.drop = set()
    network.lose = {"ack"}
    control.step()
    lost_ack = [world.state(agent, answer), list(world.ran)]
    world.later(INPUTS["poll_interval_ms"])
    network.lose = {"complete"}
    control.step()
    lost_completion = [world.state(agent, answer), list(world.ran)]
    network.lose = set()
    restarted = world.control(agent, network)
    world.later(INPUTS["poll_interval_ms"])
    restarted.step()
    stored = world.queue.outcome(agent.execution_id, answer["command_id"])
    replay = world.commands_api.route(
        "POST",
        f"/v2/executions/{agent.execution_id}/commands/{answer['command_id']}/complete",
        f"Bearer {network.token}",
        {"outcome": {"status": "failed"}},
    )
    return {
        "after_lost_ack": lost_ack,
        "after_lost_completion": lost_completion,
        "stored": [stored["state"], stored["outcome"]],
        "handlers_ran": world.ran,
        "conflicting_replay": refusal(replay),
        "calls": network.calls,
        "worker_command_ack_lag_seconds": worker_command_ack_lag_seconds(world.store, SLUG),
    }


def run_case(case, folder: Path):
    with pytest.MonkeyPatch.context() as patch:
        world = World(patch, folder)
        observed = {"a": _positive, "b": _rejection, "c": _recovery}[case](world)
        return {
            "case": f"T-SV2-LDG-05-{case.upper()}",
            "state": "passed",
            "evidence_class": "mocked Redis and server clock; fixture network; signed fixture grants",
            "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observed": observed,
        }
