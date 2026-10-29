import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scripts.swarm import command_runner, commands, lifecycle
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_server

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"


def queue_class():
    from scripts.swarm_v2.api.commands import CommandQueue

    return CommandQueue


INPUT = "ledger-lifecycle-controls.json"
EVIDENCE_CLASS = (
    "mocked: a fake Redis swarm store holds two Kubernetes workers and one local agent; page clicks enter through the "
    "ledger server's lifecycle control with the incarnation, generation and task revision a page rendered; the hive "
    "command runner consumes the queue with its terminal commands stubbed; a worker's acknowledgement and checkpoint "
    "outcome are written through the worker command queue; no ledger HTTP server, browser, worker Pod or cluster runs"
)
CASES = {"a": "T-SV2-UX-02-A", "b": "T-SV2-UX-02-B", "c": "T-SV2-UX-02-C"}


def fixture() -> dict:
    return json.loads((FIXTURES / INPUT).read_text())


class World:
    def __init__(self, data: dict, redis=None):
        import fakeredis

        self.data, self.slug, self.ran = data, data["swarm"], []
        self.store = RedisStore(redis or fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
        if redis is None:
            self.store.create(SwarmConfig(self.slug, ".", 3, 0, state="running"))
            self.seats = {row["seat"]: self.start(row) for row in data["agents"]}
            commands.publish(self.store, self.slug, {"task_revisions": data["task_revisions"]}, {})

    def start(self, row: dict) -> AgentRecord:
        seat = f"{row['seat']}@{self.slug}"
        agent = AgentRecord(self.store.next_name(self.slug, "eng"), "eng", row["task"], seat=seat)
        if row["backend"] == "kubernetes":
            target = {"pod_namespace": "workers", "pod_name": row["pod"], "pod_uid": f"uid-{row['pod']}"}
            agent = replace(agent, runtime_backend="kubernetes", runtime_target=target)
        else:
            agent = replace(agent, runtime_target={"server_id": "hive", "pid": row["pid"], "pid_start": 1})
        return self.store.start_execution(self.slug, agent)

    def offers(self) -> dict:
        return lifecycle.view(self.store, self.slug, {})["agents"]

    def click(self, seat: str, control: str, op: str, offers: dict | None = None) -> tuple[int, str]:
        name = self.seats[seat].name
        shown = (offers or self.offers())[name]
        body = {"action": "lifecycle", "control": control, "agent": name, "op": op}
        body.update({key: shown[key] for key in lifecycle.SEEN})
        with (
            patch.object(ledger_server, "swarm_store", return_value=self.store),
            patch.object(ledger_server, "swarm_status", return_value={}),
            patch.object(ledger_server.repository, "read", return_value=self.stamps()),
        ):
            try:
                ledger_server.lifecycle_control(self.slug, body, True)
            except lifecycle.Refused as refused:
                return refused.status, str(refused)
        return 200, ""

    def stamps(self) -> dict:
        rows = {f"tasks/{task}/state": {"rev": rev} for task, rev in self.data["task_revisions"].items()}
        return {"_meta": {"stamps": rows}}

    def tick(self) -> list[str]:
        with patch.object(command_runner, "run", lambda argv, env: self.ran.append(argv) or ""):
            return command_runner.consume(self.store, self.slug)

    def rows(self) -> list[dict]:
        return [row for row in commands.rows(self.store, self.slug) if row["command"] == lifecycle.COMMAND]

    def operations(self) -> dict:
        return {op["op"]: op for op in lifecycle.operations(self.store, self.slug)}

    def seat_of(self, name: str) -> str:
        return next(seat for seat, agent in self.seats.items() if agent.name == name)

    def snapshot(self) -> dict:
        keys = sorted(key for key in self.store.redis.keys("*") if not key.endswith("lifecycle-conflicts"))
        return {key: self.store.redis.dump(key) for key in keys}


def _worker_completes(world: World, seat: str, outcome: dict) -> None:
    queue, agent = queue_class()(world.store, world.slug), world.seats[seat]
    [issued] = queue.pending(agent.execution_id, 5)
    queue.accept(agent.execution_id, issued["command_id"], issued["payload_digest"])
    queue.complete(agent.execution_id, issued["command_id"], outcome)


def _drained(data: dict) -> tuple[dict, World]:
    world, drain = World(data), data["partial_drain"]
    offers = world.offers()
    answers = [
        world.click(drain["seat"], "drain", drain["op"], offers) for _ in range(data["duplicated_click"]["clicks"])
    ]
    queued = len(world.rows())
    actions = world.tick()
    _worker_completes(world, drain["seat"], drain["outcome"])
    op = world.operations()[drain["op"]]
    queue = queue_class()(world.store, world.slug)
    others = {
        seat: queue.pending(agent.execution_id, 5) for seat, agent in world.seats.items() if seat != drain["seat"]
    }
    shown = {
        "answers": answers,
        "commands_queued": queued,
        "runner": actions,
        "operation": {"control": op["control"], "state": op["state"], "error": op["error"], "worker": op["worker"]},
        "other_worker_commands": {seat: len(rows) for seat, rows in others.items()},
        "agents": {world.seat_of(a.name): a.state for a in world.store.agents(world.slug)},
        "pending": {world.seat_of(n): v for n, v in world.offers().items() if v["pending"]},
        "terminal_commands": world.ran,
    }
    return shown, world


def _case_a(data: dict) -> tuple[dict, dict, int]:
    first, world = _drained(data)
    second, _ = _drained(data)
    drain = data["partial_drain"]
    scopes = {c.action: c.scope for c in lifecycle.CONTROLS}
    checks = {
        "duplicated_click_queued_once": first["answers"] == [(200, "")] * 2 and first["commands_queued"] == 1,
        "drained_worker_reports_its_checkpoint": first["operation"]
        == {
            "control": "drain",
            "state": "issued",
            "error": "",
            "worker": {"state": "completed", "outcome": drain["outcome"]},
        },
        "other_agents_stay_active": set(first["agents"].values()) == {"working"} and len(first["agents"]) == 3,
        "no_command_reached_another_agent": set(first["other_worker_commands"].values()) == {0},
        "pending_cleared_after_settling": first["pending"] == {},
        "scopes_differ_per_control": scopes
        == {
            "attach": "terminal view",
            "pause": "one swarm",
            "drain": "one agent",
            "cancel": "one agent",
            "force_stop": "one agent",
        },
        "second_fixture_matches_the_first": second == first,
    }
    return checks, {**first, "scopes": scopes}, world_total(world)


def world_total(world: World) -> int:
    return lifecycle.view(world.store, world.slug, {})["ledger_control_conflicts_total"]


def _case_b(data: dict) -> tuple[dict, dict, int]:
    world, stale = World(data), data["stale_browser_tab"]
    old_tab = world.offers()
    agent = world.seats[stale["seat"]]
    target = {**agent.runtime_target, "pid": stale["successor_pid"]}
    successor = world.store.start_execution(
        world.slug, replace(agent, execution_id="", generation=0, runtime_target=target), agent.execution_id
    )
    before = world.snapshot()
    answer = world.click(stale["seat"], "force_stop", stale["op"], old_tab)
    after = world.snapshot()
    world.seats[stale["seat"]] = successor
    attach = data["unsupported_attach_target"]
    fresh = world.offers()[successor.name]
    unsupported = world.click(attach["seat"], "attach", attach["op"])
    current = world.store.execution(world.slug, successor.execution_id)
    checks = {
        "old_force_stop_refused_as_stale": answer[0] == 409 and answer[1].startswith("the page is stale"),
        "protected_state_unchanged": after == before,
        "successor_still_working": (current.state, current.generation) == ("working", 2),
        "no_terminal_command_ran": world.tick() == [] and world.ran == [],
        "unsupported_attach_disabled_in_the_offer": fresh["disabled"]["attach"] == "a local agent has no attach",
        "unsupported_attach_refused_without_a_command": unsupported == (422, "a local agent has no attach")
        and world.rows() == [],
        "fresh_offer_names_the_new_generation": fresh["generation"] == 2,
    }
    observed = {"stale_answer": list(answer), "unsupported_answer": list(unsupported), "successor_generation": 2}
    return checks, observed, world_total(world)


def _case_c(data: dict) -> tuple[dict, dict, int]:
    world, lost = World(data), data["lost_acknowledgement"]
    offers = world.offers()
    first = world.click(lost["seat"], "drain", lost["op"], offers)
    restarted = World(data, world.store.redis)
    restarted.seats = world.seats
    replay = restarted.click(lost["seat"], "drain", lost["op"], offers)
    record = restarted.operations()[lost["op"]]
    blocked = restarted.click(lost["seat"], "cancel", lost["next_op"], offers)
    queued_before_settling = len(restarted.rows())
    actions = restarted.tick()
    held = restarted.click(lost["seat"], "cancel", lost["next_op"], restarted.offers())
    _worker_completes(restarted, lost["seat"], lost["outcome"])
    after = restarted.click(lost["seat"], "cancel", lost["next_op"], restarted.offers())
    states = {op: row["state"] for op, row in restarted.operations().items()}
    worker = restarted.operations()[lost["op"]]["worker"]
    unresolved = (409, "an earlier control on this agent is still unresolved")
    checks = {
        "lost_acknowledgement_reconciled_by_operation_id": first == replay == (200, "") and record["state"] == "queued",
        "replay_created_no_second_command": queued_before_settling == 1,
        "another_click_refused_until_reconciled": blocked == unresolved,
        "held_until_the_worker_reports": held == unresolved,
        "next_click_accepted_after_the_checkpoint": after == (200, ""),
        "earlier_operation_kept_its_outcome": states == {lost["op"]: "issued", lost["next_op"]: "queued"}
        and worker == {"state": "completed", "outcome": lost["outcome"]},
    }
    answers = [list(answer) for answer in (first, replay, blocked, held, after)]
    observed = {"answers": answers, "runner": actions, "states": states, "worker": worker}
    return checks, observed, world_total(restarted)


RUNNERS = {"a": _case_a, "b": _case_b, "c": _case_c}


def run_case(case: str) -> dict:
    checks, observed, conflicts = RUNNERS[case](fixture())
    return {
        "case": CASES[case],
        "package": "SV2-UX-02",
        "state": "passed" if all(checks.values()) else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "checks": checks,
        "observed": json.loads(json.dumps(observed)),
        "ledger_control_conflicts_total": conflicts,
        "input_sha256": {INPUT: hashlib.sha256((FIXTURES / INPUT).read_bytes()).hexdigest()},
    }
