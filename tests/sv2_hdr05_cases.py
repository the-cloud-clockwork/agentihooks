import copy
import hashlib
import json
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path

from scripts.swarm_v2.herdr import visibility
from scripts.swarm_v2.herdr.endpoints import Endpoint, EndpointRefused
from scripts.swarm_v2.herdr.target import DELIVERIES
from scripts.swarm_v2.herdr.viewer import Viewer
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.runtime.commands import Principal, Role
from tests import sv2_hdr03_cases, sv2_hdr04_cases

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = (
    "herdr-terminal-reconnect.json",
    "herdr-pane-routing.json",
    "herdr-registrations.json",
    "herdr-capabilities.json",
)
SLUG = "sv2-hdr-05"
EVIDENCE_CLASS = (
    "mocked: a recording stand in for the Anton herdr client serves two worker herdr servers whose agents keep "
    "working on every fixture tick, answers agent get, pane read and agent prompt with the 0.9.1 reply shapes of "
    "SV2-HDR-04, times out every command to a machine whose SSH path is cut while that worker's control heartbeat "
    "continues, and advances a fake clock by a fixed time per remote command; the operator TUI is a Viewer that is "
    "closed by detach; no herdr process, SSH connection or cluster runs in these cases"
)
LAUNCHES = {
    ("workspace", "create"),
    ("workspace", "close"),
    ("tab", "create"),
    ("pane", "split"),
    ("pane", "run"),
    ("pane", "close"),
}


def fixture() -> dict:
    return json.loads((FIXTURES / "herdr-terminal-reconnect.json").read_text())


class Clock:
    def __init__(self, now_ms: int) -> None:
        self.now_ms = now_ms

    def __call__(self) -> float:
        return self.now_ms / 1000

    def advance(self, ms: int) -> None:
        self.now_ms += ms


class Workers(sv2_hdr04_cases.Fleet):
    """Two worker herdr servers whose agents work on every tick whether or not any client is attached. A cut SSH
    path times out every herdr command to that machine while the worker's control heartbeat, which runs over the
    Kubernetes exec channel, keeps beating."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        fx = fixture()
        self.clock = clock
        for execution_id, server in self.remote.items():
            server.update(pid=fx["agents"][execution_id]["pid"], work=0, name=fx["agents"][execution_id]["name"])
        self.heartbeats = {execution_id: clock.now_ms for execution_id in self.remote}
        self.cut: set[str] = set()

    def tick(self) -> None:
        self.clock.advance(fixture()["tick_ms"])
        for execution_id, server in self.remote.items():
            server["work"] += 1
            self.heartbeats[execution_id] = self.clock.now_ms

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess:
        if command[1] != "--machine":
            return super().__call__(command)
        self.clock.advance(fixture()["command_ms"])
        if command[2] in self.cut:
            self.commands.append(list(command))
            raise subprocess.TimeoutExpired(command, 30)
        if tuple(command[3:5]) != ("pane", "read"):
            return super().__call__(command)
        self.commands.append(list(command))
        server = self._machine_server(command[2])
        if server is None:
            return subprocess.CompletedProcess(command, 1, "", "unknown machine")
        return subprocess.CompletedProcess(command, 0, f"{server['name']} work {server['work']}\n", "")

    def processes(self) -> dict:
        return {
            execution_id: {
                "pid": server["pid"],
                "work": server["work"],
                "conversation_id": server["panes"][sv2_hdr04_cases.fixture()["pane_id"]]["agent_session"]["value"],
            }
            for execution_id, server in sorted(self.remote.items())
        }

    def launches(self) -> list[list[str]]:
        found = []
        for command in self.commands:
            remote = command[1] == "--machine"
            if tuple(command[3:5] if remote else command[1:3]) in LAUNCHES:
                found.append(command)
        return found


def _viewer(client: sv2_hdr04_cases.Client, workers: Workers, desired: list[Endpoint], **options) -> Viewer:
    fx, routing = fixture(), sv2_hdr04_cases.fixture()
    viewer = Viewer(client.router, workers, OPERATOR, workers.clock, fresh_ms=fx["fresh_ms"], **options)
    for endpoint in desired:
        execution_id = endpoint.scope.execution_id
        conversation = routing["remote"][execution_id]["conversation_id"]
        viewer.watch(fx["agents"][execution_id]["name"], endpoint.name, routing["pane_id"], conversation)
    return viewer


def _views(views: dict) -> dict:
    return {agent: asdict(view) for agent, view in views.items()}


def _ledger(views: dict, now_ms: int) -> dict:
    import fakeredis

    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    visibility.publish(store, SLUG, views.values())
    return visibility.read(store, SLUG, now_ms)


def _refused(action) -> dict:
    try:
        action()
    except EndpointRefused as refused:
        return {"reason": refused.reason, "message": str(refused)}
    return {}


def _journal(root: Path) -> bytes | None:
    path = root / DELIVERIES
    return path.read_bytes() if path.exists() else None


def routing_conversation(execution_id: str) -> str:
    return sv2_hdr04_cases.fixture()["remote"][execution_id]["conversation_id"]


def _names() -> tuple[str, str]:
    agents = fixture()["agents"]
    return tuple(agents[execution_id]["name"] for execution_id in sorted(agents))


def _positive_once() -> dict:
    fx = fixture()
    first_agent, lost_agent = _names()
    with tempfile.TemporaryDirectory() as tmp:
        clock = Clock(fx["start_ms"])
        workers, desired = Workers(clock), sv2_hdr03_cases.endpoints()
        client = sv2_hdr04_cases.Client(workers, Path(tmp))
        registered = client.register(desired)
        closing = _viewer(client, workers, desired)
        opened = _views(closing.refresh())
        at_close, commands_at_close = workers.processes(), len(workers.commands)
        closing.detach()
        detach_commands = len(workers.commands) - commands_at_close
        for _ in range(fx["ticks_while_closed"]):
            workers.tick()
        while_closed = workers.processes()
        reopened = _viewer(client, workers, desired)
        after_reopen = _views(reopened.refresh())
        screens = {agent: reopened.screen(agent)[0] for agent in (first_agent, lost_agent)}
        workers.cut.add(registered[fx["lost"]])
        workers.tick()
        after_cut = reopened.refresh()
        reattached = reopened.reconnect(first_agent)
        return {
            "registered": registered,
            "opened": opened,
            "at_close": at_close,
            "detach_commands": detach_commands,
            "while_closed": while_closed,
            "after_reopen": after_reopen,
            "screens_after_reopen": screens,
            "after_cut": _views(after_cut),
            "processes_after_cut": workers.processes(),
            "heartbeats": dict(workers.heartbeats),
            "now_ms": clock.now_ms,
            "reattached": asdict(reattached),
            "ledger": _ledger(after_cut, clock.now_ms),
            "launches": workers.launches(),
            "terminal_reconnect_latency_seconds": reopened.terminal_reconnect_latency_seconds(),
        }


def _positive() -> tuple[dict, bool]:
    first, second = _positive_once(), _positive_once()
    fx, routing = fixture(), sv2_hdr04_cases.fixture()
    first_agent, lost_agent = _names()
    kept = fx["agents"].keys() - {fx["lost"]}
    checks = {
        "both_views_open_live": {view["state"] for view in first["opened"].values()} == {visibility.LIVE},
        "closing_the_tui_runs_no_herdr_command": first["detach_commands"] == 0,
        "both_agents_keep_working_while_the_tui_is_closed": all(
            first["while_closed"][execution_id]["work"]
            == first["at_close"][execution_id]["work"] + fx["ticks_while_closed"]
            and first["while_closed"][execution_id]["pid"] == first["at_close"][execution_id]["pid"]
            and first["while_closed"][execution_id]["conversation_id"]
            == first["at_close"][execution_id]["conversation_id"]
            for execution_id in fx["agents"]
        ),
        "reopen_shows_the_same_conversations_and_their_progress": {
            agent: view["state"] for agent, view in first["after_reopen"].items()
        }
        == {first_agent: visibility.LIVE, lost_agent: visibility.LIVE}
        and first["screens_after_reopen"]
        == {name: f"{name} work {fx['ticks_while_closed']}\n" for name in (first_agent, lost_agent)},
        "lost_path_leaves_the_other_view_live": first["after_cut"][first_agent]["state"] == visibility.LIVE,
        "lost_path_view_is_cached_never_gone": first["after_cut"][lost_agent]["state"] == visibility.CACHED
        and first["after_cut"][lost_agent]["reason"] == "unreachable"
        and first["after_cut"][lost_agent]["live_at"] == first["after_reopen"][lost_agent]["live_at"],
        "lost_worker_keeps_working_and_heartbeating": first["processes_after_cut"][fx["lost"]]["work"]
        == fx["ticks_while_closed"] + 1
        and first["heartbeats"][fx["lost"]] == first["heartbeats"][next(iter(kept))],
        "explicit_reattach_keeps_pane_incarnation_and_conversation": first["reattached"]["pane_id"]
        == routing["pane_id"]
        and first["reattached"]["conversation_id"] == routing["remote"][next(iter(kept))]["conversation_id"],
        "ledger_shows_live_and_cached": {agent: row["state"] for agent, row in first["ledger"].items()}
        == {first_agent: visibility.LIVE, lost_agent: visibility.CACHED},
        "ledger_rows_carry_no_screen_text": all("work" not in json.dumps(row) for row in first["ledger"].values()),
        "nothing_launched_stopped_or_closed": first["launches"] == [],
        "independent_repeat_identical": first == second,
    }
    return {"checks": checks, "first": first, "second_identical": first == second}, all(checks.values())


def _rejection_once() -> dict:
    fx = fixture()
    first_agent, lost_agent = _names()
    with tempfile.TemporaryDirectory() as tmp:
        root, clock = Path(tmp), Clock(fx["start_ms"])
        workers, desired = Workers(clock), sv2_hdr03_cases.endpoints()
        client = sv2_hdr04_cases.Client(workers, root)
        registered = client.register(desired)
        commands = len(workers.commands)
        master = Principal("master@a1b2c3-0001", Role.MASTER)
        unauthorized = _refused(lambda: Viewer(client.router, workers, master, clock))
        unauthorized["commands"] = len(workers.commands) - commands
        viewer = _viewer(client, workers, desired)
        never = _refused(lambda: viewer.send(first_agent, "agent_prompt", "op-never", "before any check"))
        viewer.refresh()
        workers.cut.add(registered[fx["lost"]])
        cached = _views(viewer.refresh())
        before = (workers.received(), len(workers.mutations()), _journal(root))
        refused = _refused(lambda: viewer.send(lost_agent, "agent_prompt", "op-cached", "into a cached view"))
        unchanged = (workers.received(), len(workers.mutations()), _journal(root)) == before
        clock.advance(fx["fresh_ms"] + 1)
        expired = _views(viewer.views())
        expired_send = _refused(lambda: viewer.send(first_agent, "agent_prompt", "op-expired", "after the window"))
        published = _ledger({first_agent: viewer.watched[first_agent].view}, clock.now_ms)
        workers.cut.clear()
        still_refused = _refused(lambda: viewer.send(lost_agent, "agent_prompt", "op-restored", "without a check"))
        viewer.reconnect(lost_agent)
        corrected = viewer.send(lost_agent, "agent_prompt", "op-corrected", "after reconnect")
        return {
            "unauthorized_view": unauthorized,
            "never_live_send": never,
            "cached": cached,
            "cached_send": refused,
            "cached_send_mutated_nothing": unchanged,
            "expired": expired,
            "expired_send": expired_send,
            "published_after_window": published,
            "restored_without_a_check": still_refused,
            "corrected_returncode": corrected.returncode,
            "received": workers.received(),
        }


def _rejection() -> tuple[dict, bool]:
    first, second = _rejection_once(), _rejection_once()
    fx = fixture()
    first_agent, lost_agent = _names()
    refusals = [first[name] for name in ("never_live_send", "cached_send", "expired_send", "restored_without_a_check")]
    checks = {
        "only_the_operator_role_opens_a_view": first["unauthorized_view"]
        == {"reason": "unauthorized", "message": "only the operator role opens a terminal view", "commands": 0},
        "every_send_into_an_unverified_view_is_refused": [entry.get("reason") for entry in refusals]
        == ["unverified"] * 4,
        "a_never_checked_view_reads_unverified": first["never_live_send"]["message"]
        == f"{first_agent}'s terminal is unverified; reconnect before sending input",
        "a_cut_path_reads_cached": first["cached"][lost_agent]["state"] == visibility.CACHED,
        "the_refused_send_mutated_nothing": first["cached_send_mutated_nothing"],
        "a_live_view_past_its_window_reads_cached": {agent: view["state"] for agent, view in first["expired"].items()}
        == {first_agent: visibility.CACHED, lost_agent: visibility.CACHED}
        and first["expired"][first_agent]["reason"] == "stale",
        "the_ledger_judges_freshness_at_read": first["published_after_window"][first_agent]["state"]
        == visibility.CACHED,
        "no_screen_text_in_refusals": not any("work" in entry["message"] for entry in refusals),
        "a_corrected_input_needs_a_new_request": first["corrected_returncode"] == 0
        and first["received"][fx["lost"]] == ["after reconnect"]
        and first["received"]["local"] == [],
        "independent_repeat_identical": first == second,
    }
    return {"checks": checks, "first": first, "second_identical": first == second}, all(checks.values())


def _recovery_once() -> dict:
    fx = fixture()
    _, lost_agent = _names()
    with tempfile.TemporaryDirectory() as tmp:
        root, clock = Path(tmp), Clock(fx["start_ms"])
        workers, desired = Workers(clock), sv2_hdr03_cases.endpoints()
        client = sv2_hdr04_cases.Client(workers, root)
        registered = client.register(desired)
        viewer = _viewer(client, workers, desired)
        viewer.refresh()
        viewer.send(lost_agent, "agent_prompt", "op-1", "first prompt")
        accepted = workers.processes()
        workers.cut.add(registered[fx["lost"]])
        workers.tick()
        interrupted = _refused(lambda: viewer.reconnect(lost_agent))
        workers.cut.clear()
        restarted = sv2_hdr04_cases.Client(workers, root)
        restored = restarted.register(desired)
        reopened = _viewer(restarted, workers, desired)
        commands_before, mutations_before = len(workers.commands), len(workers.mutations())
        reattached = reopened.reconnect(lost_agent)
        reconnect_commands = [
            command[3:5]
            for command in workers.commands[commands_before:]
            if command[1] == "--machine" and tuple(command[3:5]) in sv2_hdr04_cases.PANE_COMMANDS
        ]
        reconnect_mutations = len(workers.mutations()) - mutations_before
        replayed = reopened.send(lost_agent, "agent_prompt", "op-1", "first prompt")
        pane = workers.remote[fx["lost"]]["panes"][sv2_hdr04_cases.fixture()["pane_id"]]
        pane["agent_session"] = {"kind": "id", "value": fx["replacement_conversation_id"]}
        replaced = _refused(lambda: reopened.reconnect(lost_agent))
        replaced_view = asdict(reopened.views()[lost_agent])
        pane["agent_session"] = {"kind": "id", "value": routing_conversation(fx["lost"])}
        reopened.reconnect(lost_agent)
        restarted.register(sv2_hdr03_cases.endpoints(desired[0].scope.generation, desired[1].scope.generation + 1))
        stale_send = _refused(lambda: reopened.send(lost_agent, "agent_prompt", "op-4", "from the old attempt"))
        stale = _refused(lambda: reopened.reconnect(lost_agent))
        return {
            "registered": registered,
            "accepted": accepted,
            "interrupted": interrupted,
            "restored": restored,
            "reattached": asdict(reattached),
            "reconnect_commands": reconnect_commands,
            "reconnect_mutations": reconnect_mutations,
            "after_reconnect": workers.processes(),
            "replayed": replayed,
            "replaced": replaced,
            "replaced_view": replaced_view,
            "stale": stale,
            "stale_send": stale_send,
            "received": workers.received(),
            "launches": workers.launches(),
            "journal": json.loads((root / DELIVERIES).read_text()),
            "terminal_reconnect_latency_seconds": reopened.terminal_reconnect_latency_seconds(),
        }


def _recovery() -> tuple[dict, bool]:
    first, second = _recovery_once(), _recovery_once()
    fx, routing = fixture(), sv2_hdr04_cases.fixture()
    lost = fx["lost"]
    checks = {
        "an_unreachable_reconnect_is_refused_not_replaced": first["interrupted"].get("reason") == "unreachable",
        "restart_keeps_the_saved_machines": first["restored"] == first["registered"],
        "reconnect_attaches_the_original_pane_and_conversation": first["reattached"]["pane_id"] == routing["pane_id"]
        and first["reattached"]["conversation_id"] == routing["remote"][lost]["conversation_id"]
        and first["reattached"]["incarnation"] == sv2_hdr03_cases.endpoints()[1].incarnation,
        "the_original_process_still_runs": first["after_reconnect"][lost]["pid"] == first["accepted"][lost]["pid"]
        and first["after_reconnect"][lost]["work"] == first["accepted"][lost]["work"] + 1,
        "reconnect_only_reads": first["reconnect_commands"] == [["agent", "get"], ["pane", "read"]]
        and first["reconnect_mutations"] == 0,
        "replay_sends_nothing": first["replayed"] is None and first["received"][lost] == ["first prompt"],
        "a_replaced_conversation_is_refused": first["replaced"].get("reason") == "conversation_mismatch"
        and first["replaced_view"]["state"] == visibility.CACHED,
        "nothing_launched_stopped_or_closed": first["launches"] == [],
        "an_older_attempt_cannot_act_for_a_newer_generation": first["stale"].get("reason") == "stale_incarnation"
        and first["stale_send"].get("reason") == "stale_incarnation"
        and first["received"][lost] == ["first prompt"],
        "durable_journal_records_the_delivery_once": {
            key: entry["state"] for key, entry in first["journal"]["deliveries"].items()
        }
        == {"op-1": "sent"},
        "latency_measured_on_the_fixture_clock": first["terminal_reconnect_latency_seconds"]
        == [4 * fx["command_ms"] / 1000, 4 * fx["command_ms"] / 1000],
        "independent_repeat_identical": first == second,
    }
    return {"checks": checks, "first": first, "second_identical": first == second}, all(checks.values())


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-HDR-05-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": copy.deepcopy(observed),
    }
