import subprocess
import time
from types import SimpleNamespace

import pytest

from scripts.swarm_v2.herdr.endpoints import EndpointRefused
from scripts.swarm_v2.herdr.viewer import Viewer, Watched
from scripts.swarm_v2.herdr.visibility import CACHED, FRESH_MS, LIVE, UNVERIFIED, View
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.runtime.commands import Principal, Role
from tests import sv2_hdr03_cases, sv2_hdr04_cases, sv2_hdr05_cases
from tests.sv2_hdr05_cases import Clock, Workers

FX = sv2_hdr05_cases.fixture()
ROUTING = sv2_hdr04_cases.fixture()
PANE = ROUTING["pane_id"]
EXE_A, EXE_B = sorted(FX["agents"])
AGENT_A, AGENT_B = FX["agents"][EXE_A]["name"], FX["agents"][EXE_B]["name"]
CONVERSATION = {EXE_A: ROUTING["remote"][EXE_A]["conversation_id"], EXE_B: ROUTING["remote"][EXE_B]["conversation_id"]}


def rig(tmp_path, run=None, **options):
    clock = Clock(FX["start_ms"])
    workers, desired = Workers(clock), sv2_hdr03_cases.endpoints()
    client = sv2_hdr04_cases.Client(workers, tmp_path)
    registered = client.register(desired)
    viewer = Viewer(client.router, run or workers, OPERATOR, clock, fresh_ms=FX["fresh_ms"], **options)
    label = {endpoint.scope.execution_id: endpoint.name for endpoint in desired}
    return SimpleNamespace(
        clock=clock, workers=workers, desired=desired, client=client, registered=registered, viewer=viewer, label=label
    )


def watch(r, *executions):
    for execution_id in executions or (EXE_A, EXE_B):
        name = FX["agents"][execution_id]["name"]
        r.viewer.watch(name, r.label[execution_id], PANE, CONVERSATION[execution_id])


def refused(action) -> tuple[str, str]:
    with pytest.raises(EndpointRefused) as caught:
        action()
    return caught.value.reason, str(caught.value)


def remote_verbs(workers, since=0):
    return [
        (command[2], *command[3:5])
        for command in workers.commands[since:]
        if command[1] == "--machine" and tuple(command[3:5]) in sv2_hdr04_cases.PANE_COMMANDS
    ]


def test_defaults_read_the_wall_clock_with_aggregation_on(tmp_path):
    r = rig(tmp_path)
    plain = Viewer(r.client.router, r.workers, OPERATOR)
    assert (plain.clock, plain.fresh_ms, plain.aggregation) == (time.time, FRESH_MS, True)
    assert plain.watched == {}
    assert plain.terminal_reconnect_latency_seconds() == []


def test_only_the_operator_role_opens_a_terminal_view(tmp_path):
    r = rig(tmp_path)
    since = len(r.workers.commands)
    master = Principal("operator", Role.MASTER)
    assert refused(lambda: Viewer(r.client.router, r.workers, master)) == (
        "unauthorized",
        "only the operator role opens a terminal view",
    )
    assert len(r.workers.commands) == since


def test_watch_resolves_the_target_through_the_registration_record_and_starts_unverified(tmp_path):
    r = rig(tmp_path)
    view = r.viewer.watch(AGENT_A, r.label[EXE_A], PANE, CONVERSATION[EXE_A])
    endpoint = r.desired[0]
    assert view == View(AGENT_A, endpoint.name, PANE, UNVERIFIED, fresh_ms=FX["fresh_ms"])
    target = r.client.router.resolve(endpoint.name, PANE, CONVERSATION[EXE_A])
    assert r.viewer.watched[AGENT_A] == Watched(target, view)
    narrow = Viewer(r.client.router, r.workers, OPERATOR, r.clock, fresh_ms=10)
    assert narrow.watch(AGENT_A, r.label[EXE_A], PANE, CONVERSATION[EXE_A]).fresh_ms == 10


def test_watch_needs_a_conversation_and_a_registered_machine(tmp_path):
    r = rig(tmp_path)
    assert refused(lambda: r.viewer.watch(AGENT_A, r.label[EXE_A], PANE, "")) == (
        "conversation_required",
        f"{AGENT_A}'s terminal view needs its conversation id",
    )
    reason, _ = refused(lambda: r.viewer.watch(AGENT_A, ROUTING["agent"], PANE, CONVERSATION[EXE_A]))
    assert reason == "unregistered"
    assert r.viewer.watched == {}


def test_refresh_confirms_each_conversation_and_keeps_its_screen(tmp_path):
    r = rig(tmp_path)
    watch(r, EXE_B, EXE_A)
    since, started = len(r.workers.commands), r.clock.now_ms
    views = r.viewer.refresh()
    assert list(views) == [AGENT_A, AGENT_B]
    profiles = [r.registered[EXE_A], r.registered[EXE_B]]
    assert [verb[0] for verb in remote_verbs(r.workers, since)] == [profiles[0], profiles[0], profiles[1], profiles[1]]
    assert [verb[1:] for verb in remote_verbs(r.workers, since)] == [("agent", "get"), ("pane", "read")] * 2
    first = views[AGENT_A]
    assert (first.state, first.reason) == (LIVE, "")
    assert first.live_at == first.checked_at == started
    assert r.viewer.screen(AGENT_A) == (f"{AGENT_A} work 0\n", first)


def test_a_lost_machine_is_cached_with_its_last_screen_while_the_other_stays_live(tmp_path):
    r = rig(tmp_path)
    watch(r)
    before = r.viewer.refresh()
    r.workers.cut.add(r.registered[EXE_B])
    r.workers.tick()
    views = r.viewer.refresh()
    assert views[AGENT_A].state == LIVE
    lost = views[AGENT_B]
    assert (lost.state, lost.reason, lost.live_at) == (CACHED, "unreachable", before[AGENT_B].live_at)
    assert lost.checked_at > lost.live_at
    assert r.viewer.screen(AGENT_B) == (f"{AGENT_B} work 0\n", lost)
    assert r.viewer.screen(AGENT_A)[0] == f"{AGENT_A} work 1\n"
    assert r.viewer.watched[AGENT_B].view.state == CACHED
    r.workers.cut.clear()
    back = r.viewer.refresh()[AGENT_B]
    assert (back.state, back.reason) == (LIVE, "")


def test_a_view_that_never_answered_reads_unverified_with_its_reason(tmp_path):
    r = rig(tmp_path)
    watch(r, EXE_B)
    r.workers.cut.add(r.registered[EXE_B])
    started = r.clock.now_ms
    view = r.viewer.refresh()[AGENT_B]
    assert (view.state, view.reason, view.live_at, view.checked_at) == (UNVERIFIED, "unreachable", 0, started)


def test_an_unreachable_capability_probe_reads_unreachable(tmp_path):
    r = rig(tmp_path)
    r.workers.cut.add(r.registered[EXE_A])
    restarted = sv2_hdr04_cases.Client(r.workers, tmp_path)
    viewer = Viewer(restarted.router, r.workers, OPERATOR, r.clock, fresh_ms=FX["fresh_ms"])
    viewer.watch(AGENT_A, r.label[EXE_A], PANE, CONVERSATION[EXE_A])
    assert viewer.refresh()[AGENT_A].reason == "unreachable"


def broken(command):
    raise OSError("broken pipe")


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (broken, f"{PANE} did not answer pane_read"),
        (
            lambda command: subprocess.CompletedProcess(command, 1, "", "pane not found"),
            f"herdr exited 1 for pane_read on {PANE}",
        ),
    ],
)
def test_a_failed_or_refused_read_is_unreachable(tmp_path, failure, message):
    holder = {}

    def run(command):
        if command[3:5] == ["pane", "read"]:
            return failure(command)
        return holder["workers"](command)

    r = rig(tmp_path, run=run)
    holder["workers"] = r.workers
    watch(r, EXE_A)
    view = r.viewer.refresh()[AGENT_A]
    assert (view.state, view.reason) == (UNVERIFIED, "unreachable")
    assert refused(lambda: r.viewer.reconnect(AGENT_A)) == ("unreachable", message)


@pytest.mark.parametrize(
    ("session", "reason", "message"),
    [
        ({"kind": "none"}, "conversation_unconfirmed", f"{PANE} reported no conversation"),
        ({"kind": "id", "value": ""}, "conversation_unconfirmed", f"{PANE} reported no conversation"),
        (
            {"kind": "id", "value": "44444444-4444-4444-8444-444444444444"},
            "conversation_mismatch",
            f"{PANE} holds another conversation",
        ),
    ],
)
def test_a_pane_without_the_expected_conversation_is_not_live(tmp_path, session, reason, message):
    r = rig(tmp_path)
    watch(r, EXE_A)
    r.workers.remote[EXE_A]["panes"][PANE]["agent_session"] = session
    since = len(r.workers.commands)
    view = r.viewer.refresh()[AGENT_A]
    assert (view.state, view.reason) == (UNVERIFIED, reason)
    assert [verb[1:] for verb in remote_verbs(r.workers, since)] == [("agent", "get")]
    assert refused(lambda: r.viewer.reconnect(AGENT_A)) == (reason, message)


def test_a_reply_that_is_not_json_is_unconfirmed(tmp_path):
    holder = {}

    def run(command):
        if command[3:5] == ["agent", "get"]:
            return subprocess.CompletedProcess(command, 0, "not json", "")
        return holder["workers"](command)

    r = rig(tmp_path, run=run)
    holder["workers"] = r.workers
    watch(r, EXE_A)
    assert r.viewer.refresh()[AGENT_A].reason == "conversation_unconfirmed"


def test_input_reaches_only_a_live_view(tmp_path):
    r = rig(tmp_path)
    watch(r)
    assert refused(lambda: r.viewer.send(AGENT_A, "agent_prompt", "op-1", "x")) == (
        "unverified",
        f"{AGENT_A}'s terminal is unverified; reconnect before sending input",
    )
    r.viewer.refresh()
    r.workers.cut.add(r.registered[EXE_B])
    r.viewer.refresh()
    assert refused(lambda: r.viewer.send(AGENT_B, "agent_prompt", "op-2", "x")) == (
        "unverified",
        f"{AGENT_B}'s terminal is cached; reconnect before sending input",
    )
    done = r.viewer.send(AGENT_A, "agent_prompt", "op-3", "hello")
    assert done.returncode == 0
    assert r.workers.received() == {"local": [], EXE_A: ["hello"], EXE_B: []}
    assert refused(lambda: r.viewer.send("someone", "agent_prompt", "op-4", "x")) == (
        "unwatched",
        "someone has no terminal in this view",
    )


def test_a_live_view_refuses_input_once_its_window_passes(tmp_path):
    r = rig(tmp_path)
    watch(r, EXE_A)
    checked = r.viewer.refresh()[AGENT_A].checked_at
    r.clock.now_ms = checked + FX["fresh_ms"]
    assert r.viewer.views()[AGENT_A].state == LIVE
    r.clock.now_ms += 1
    assert r.viewer.views()[AGENT_A].state == CACHED
    assert refused(lambda: r.viewer.send(AGENT_A, "agent_prompt", "op-1", "late"))[0] == "unverified"


def test_reconnect_confirms_the_original_pane_and_measures_its_latency(tmp_path):
    r = rig(tmp_path)
    watch(r)
    r.viewer.refresh()
    r.workers.cut.add(r.registered[EXE_B])
    r.viewer.refresh()
    assert refused(lambda: r.viewer.reconnect(AGENT_B)) == ("unreachable", f"{PANE} did not answer agent_get")
    assert r.viewer.terminal_reconnect_latency_seconds() == []
    r.workers.cut.clear()
    target = r.viewer.reconnect(AGENT_B)
    assert target == r.client.router.resolve(r.label[EXE_B], PANE, CONVERSATION[EXE_B])
    assert r.viewer.views()[AGENT_B].state == LIVE
    latencies = r.viewer.terminal_reconnect_latency_seconds()
    assert latencies == [4 * FX["command_ms"] / 1000]
    latencies.append(9.0)
    assert r.viewer.terminal_reconnect_latency_seconds() == [4 * FX["command_ms"] / 1000]
    assert r.workers.launches() == []


def test_reconnect_refuses_a_replaced_conversation_and_keeps_the_view_cached(tmp_path):
    r = rig(tmp_path)
    watch(r, EXE_A)
    r.viewer.refresh()
    r.workers.remote[EXE_A]["panes"][PANE]["agent_session"] = {"kind": "id", "value": FX["replacement_conversation_id"]}
    assert refused(lambda: r.viewer.reconnect(AGENT_A))[0] == "conversation_mismatch"
    assert r.viewer.views()[AGENT_A].state == CACHED
    assert r.workers.mutations() == []
    assert refused(lambda: r.viewer.reconnect("someone"))[0] == "unwatched"


def test_aggregation_off_refreshes_nothing_while_explicit_attach_and_input_still_work(tmp_path):
    r = rig(tmp_path, aggregation=False)
    watch(r)
    since = len(r.workers.commands)
    views = r.viewer.refresh()
    assert len(r.workers.commands) == since
    assert {view.state for view in views.values()} == {UNVERIFIED}
    r.viewer.reconnect(AGENT_A)
    assert r.viewer.send(AGENT_A, "agent_prompt", "op-1", "direct").returncode == 0
    assert r.workers.received()[EXE_A] == ["direct"]
    assert r.viewer.views()[AGENT_B].state == UNVERIFIED


def test_detach_runs_no_herdr_command_and_forgets_every_target(tmp_path):
    r = rig(tmp_path)
    watch(r)
    r.viewer.refresh()
    since = len(r.workers.commands)
    r.viewer.detach()
    assert len(r.workers.commands) == since
    assert r.viewer.views() == {}
    assert r.viewer.refresh() == {}
    assert refused(lambda: r.viewer.screen(AGENT_A))[0] == "unwatched"


def test_forget_drops_one_target_without_a_herdr_command_and_ignores_an_unwatched_agent(tmp_path):
    r = rig(tmp_path)
    watch(r)
    since = len(r.workers.commands)

    r.viewer.forget(AGENT_A)
    r.viewer.forget(AGENT_A)

    assert len(r.workers.commands) == since
    assert sorted(r.viewer.watched) == [AGENT_B]
