"""The host steps run the tick wrapper's relay, probes, health, commands, phases, refresh, intent and reboot calls in today's order."""

from types import SimpleNamespace
from typing import Any

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.hive import sweep
from scripts.inbox import relay
from scripts.swarm import (
    command_runner,
    delivery,
    hive_ledger,
    incidents,
    ledger_client,
    ledger_events,
    ledger_probe,
    ledger_watchdog,
    outage,
    reboot,
    refresh_job,
    timing,
)
from scripts.swarm.health import ordering_dependencies, prompt_backlog, urgent_hold
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, CallStep, actions, budgets, host

pytestmark = pytest.mark.unit


class _State:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state

    def read(self, domain: Domain, key: str) -> object | None:
        return self.state.get(key) if domain == PASS else None


class _Slots:
    def __init__(self, redis: object) -> None:
        self.refresh_reported = ("refresh reported", redis)


STORE = SimpleNamespace(redis="redis")


def _pass(box: dict[str, Any] | None = None, **extra: object) -> dict[str, Any]:
    state: dict[str, Any] = {
        "slug": "sw",
        "store": STORE,
        "ledger": "ledger",
        "runtime": "runtime",
        "box": {} if box is None else box,
    }
    return state | extra


def _cli(calls: list[tuple[Any, ...]], raises: object = None) -> SimpleNamespace:
    def skip_refused(function: Any, *args: Any) -> list[str]:
        calls.append(("skip", function, *args))
        if function is raises:
            raise RuntimeError("boom")
        return [f"ran {len(calls)}"]

    def intent_pass(*args: Any) -> tuple[str, str]:
        calls.append(("intent", *args))
        return ("view", "checked")

    def phase_steps(*args: Any) -> list[str]:
        calls.append(("phases", *args))
        return ["phase ticked"]

    return SimpleNamespace(
        now_ms=lambda: 7,
        skip_refused=skip_refused,
        _intent_pass=intent_pass,
        _phase_steps=phase_steps,
        _planning_after_phase=lambda *args: calls.append(("after", *args)) or ["planned again"],
        relay=relay,
        refresh_job=refresh_job,
    )


def _run(group: str, calls: list[tuple[Any, ...]], state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    cli = _cli(calls, state.pop("raises", None))
    loaded: list[str] = []

    def call(function: Any, *args: Any) -> list[str]:
        calls.append(("call", function, *args))
        return [f"ran {len(calls)}"]

    def load(name: str) -> SimpleNamespace:
        loaded.append(name)
        return cli

    monkeypatch.setattr(host, "import_module", load)
    monkeypatch.setattr(timing, "call", call)
    monkeypatch.setattr(host, "LaunchSlots", _Slots)
    monkeypatch.setattr(outage, "providers", lambda *args: calls.append(("providers", *args)))
    monkeypatch.setattr(outage, "share", lambda *args: calls.append(("share", *args)))
    monkeypatch.setattr(hive_ledger, "observe", lambda *args: calls.append(("hive", *args)))
    found = actions(engine.run_group(group, Context(_State(state), Deadline.after(60.0))))
    assert set(loaded) <= {"scripts.swarm.cli"}
    return found


def test_the_host_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert [(step.name, step.group) for step in host.STEPS] == [
        ("inbox relay", "wrapper probes"),
        ("launch slots", "wrapper probes"),
        ("outage probes", "wrapper probes"),
        ("hive ledger", "wrapper probes"),
        ("ledger health", "wrapper probes"),
        ("host pressure", "wrapper health"),
        ("ledger watchdog", "wrapper health"),
        ("ledger probe", "wrapper health"),
        ("prompt backlog", "wrapper health"),
        ("command runner", "wrapper commands"),
        ("urgent hold", "wrapper phases"),
        ("dependency audit", "wrapper phases"),
        ("planning and phase pass", "wrapper phases"),
        ("planning after phase", "wrapper phases"),
        ("refresh job", "wrapper refresh"),
        ("intent check", "wrapper intent"),
        ("reboot pass", "wrapper reboot"),
        ("outbox migration", "wrapper relay"),
        ("page relay", "wrapper relay"),
        ("tick view", "wrapper view"),
    ]
    registered = [step for wave in registry.waves() for step in wave]
    for step in host.STEPS:
        assert step in registered
        slow = {"hive ledger", "host pressure", "ledger watchdog", "ledger probe", "prompt backlog"}
        lane = engine.SLOW if step.name in slow else CallStep.lane
        assert (step.lane, step.budget_s) == (lane, budgets.timing_of(step.name)[0])


def test_the_probes_relay_the_inbox_refresh_slots_and_probe_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper probes", calls, _pass(), monkeypatch) == []
    assert calls == [
        ("call", relay.relay, "redis"),
        ("call", ("refresh reported", "redis"), 7),
        ("providers", STORE, "sw", "runtime", 7),
        ("share", STORE, "sw", 7),
        ("hive", STORE, "sw", 7),
    ]


def test_the_health_steps_return_pressure_watchdog_and_probe_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper health", calls, _pass(), monkeypatch) == ["ran 1", "ran 2", "ran 3", "ran 5"]
    assert calls == [
        ("skip", incidents.host_pressure, STORE, "sw"),
        ("call", ledger_watchdog.watch, STORE, "sw", "ledger", "runtime"),
        ("call", ledger_probe.observe, STORE, "sw", "ledger", "runtime", 7),
        ("call", prompt_backlog.observe, STORE, "sw", 0),
        ("call", sweep.sweep, STORE, 7),
    ]


def test_the_prompt_backlog_observes_at_whole_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(timing, "call", lambda *args: calls.append(args))
    cli = SimpleNamespace(now_ms=lambda: 2_000_999)
    host._prompt_backlog(cli, _pass().get)
    assert calls == [(prompt_backlog.observe, STORE, "sw", 2000)]


def test_the_command_runner_consumes_the_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper commands", calls, _pass(), monkeypatch) == ["ran 1"]
    assert calls == [("call", command_runner.consume, STORE, "sw")]


class _Client(ledger_client.LedgerClient):
    def __init__(self, metadata: object) -> None:
        self.found = metadata

    def metadata(self, slug: str) -> object:
        return self.found


def _health(monkeypatch: pytest.MonkeyPatch, healthy: bool, metadata: object) -> tuple[dict[str, Any], list[object]]:
    seen: list[object] = []

    def ledger_health(slug: str, now: int, probe: Any) -> bool:
        seen.extend([slug, now, probe()])
        return healthy

    monkeypatch.setattr(outage, "ledger_health", ledger_health)
    box: dict[str, Any] = {}
    _run("wrapper probes", [], _pass(box, ledger=_Client(metadata)), monkeypatch)
    return box, seen


def test_a_failed_ledger_health_probe_stops_the_tick_through_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    box, seen = _health(monkeypatch, False, None)
    assert box == {"stop": ["Ledger health probe failed"]}
    assert seen == ["sw", 7, False]


def test_a_healthy_ledger_leaves_the_tick_running(monkeypatch: pytest.MonkeyPatch) -> None:
    box, seen = _health(monkeypatch, True, {})
    assert box == {}
    assert seen == ["sw", 7, True]


def test_a_ledger_that_is_no_client_skips_the_health_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outage, "ledger_health", lambda *args: pytest.fail("probed"))
    box: dict[str, Any] = {}
    _run("wrapper probes", [], _pass(box), monkeypatch)
    assert box == {}


def test_the_phase_steps_observe_the_urgent_hold_and_dependency_audit_then_run_the_planning_and_phase_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    state = _pass(box, inbox="inbox", doc="doc")
    assert _run("wrapper phases", calls, state, monkeypatch) == ["phase ticked", "planned again"]
    assert calls == [
        ("call", urgent_hold.observe, STORE, "sw", "doc", 7),
        ("call", ordering_dependencies.observe, STORE, "sw", "doc"),
        ("phases", "inbox", STORE, "sw", "doc", "ledger"),
        ("after", "inbox", STORE, "sw", "ledger", box),
    ]


def test_no_host_step_wraps_the_tick_and_the_reboot_site_holds_only_the_reboot_pass() -> None:
    registered = [step for wave in registry.waves() for step in wave]
    assert "tick call" not in {step.name for step in registered}
    assert "tick call" not in budgets.FLOORS
    assert [step.name for step in engine.group_steps("wrapper reboot")] == ["reboot pass"]


class _Agent:
    def __init__(self, name: str, state: str) -> None:
        self.name, self.state = name, state


def test_the_relay_migrates_the_outbox_then_relays_live_agents_to_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    live, done = _Agent("a", "working"), _Agent("b", "finished")
    store = SimpleNamespace(redis="redis", agents=lambda slug: [live, done])

    def call(function: Any, *args: Any) -> Any:
        calls.append(("call", function, *args))
        return function(*args) if function == store.agents else ["migrated"]

    monkeypatch.setattr(host, "import_module", lambda name: _cli(calls))
    monkeypatch.setattr(timing, "call", call)
    box: dict[str, Any] = {}
    state = _pass(box, store=store, inbox="inbox")
    found = actions(engine.run_group("wrapper relay", Context(_State(state), Deadline.after(60.0))))
    assert found == []
    assert calls == [
        ("call", delivery.migrate_outbox, store, "sw", "inbox"),
        ("call", store.agents, "sw"),
        ("skip", delivery.relay_to_page, "inbox", "sw", [live], "ledger"),
    ]
    assert box == {"agents": [live]}


def test_the_tick_view_keeps_the_view_in_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    state = _pass(box, inbox="inbox", doc="doc", view="judged")
    assert _run("wrapper view", calls, state, monkeypatch) == []
    assert calls == [("call", ledger_events.tick_view, "inbox", STORE, "sw", "doc", "judged")]
    assert box == {"view": ["ran 1"]}


def test_the_refresh_job_starts_only_on_a_scheduled_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper refresh", calls, _pass(scheduled=False), monkeypatch) == []
    assert calls == []
    assert _run("wrapper refresh", calls, _pass(scheduled=True), monkeypatch) == []
    assert calls == [("call", refresh_job.start, STORE, "sw")]


def test_the_intent_check_keeps_its_judgement_in_its_judging_box(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    judging: dict[str, Any] = {"doc": "judged doc", "config": "judged config"}
    state = _pass(box, inbox="inbox", doc="doc", config="config", judging=judging)
    assert _run("wrapper intent", calls, state, monkeypatch) == []
    assert calls == [("intent", "inbox", STORE, "sw", "judged doc", "ledger", "judged config")]
    assert judging == {"doc": "judged doc", "config": "judged config", "judged": ("view", "checked")}
    assert box == {}


def test_the_intent_check_holds_what_it_raises_in_its_judging_box(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    judging: dict[str, Any] = {}

    def boom(cli: Any, get: Any) -> None:
        calls.append(("boom", cli))
        raise RuntimeError("judge down")

    monkeypatch.setattr(host, "import_module", lambda name: "cli")
    assert host.held(boom, host.JUDGING)(None, {"box": box, host.JUDGING: judging}.get) == []
    assert host.held(boom, host.JUDGING)(None, {"box": box, host.JUDGING: judging}.get) == []
    assert calls == [("boom", "cli")]
    assert box == {}
    assert isinstance(judging["raised"], RuntimeError)


def test_the_reboot_pass_boots_with_the_fenced_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper reboot", calls, _pass(), monkeypatch) == ["ran 1"]
    assert calls == [("skip", reboot.boot_pass, "sw", STORE, "ledger", "runtime", 7)]


def test_a_raising_step_skips_the_rest_and_raises_at_the_site(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    state = _pass(box, raises=incidents.host_pressure)
    assert _run("wrapper health", calls, state, monkeypatch) == []
    assert calls == [("skip", incidents.host_pressure, STORE, "sw")]
    assert list(box) == ["raised"]
    with pytest.raises(RuntimeError, match="^boom$"):
        host.raise_held(box)


def test_a_held_system_exit_is_raised_again(monkeypatch: pytest.MonkeyPatch) -> None:
    def leave(cli: Any, get: Any) -> Any:
        raise SystemExit(3)

    monkeypatch.setattr(host, "import_module", lambda name: None)
    box: dict[str, Any] = {}
    assert host.held(leave)(None, {"box": box}.get) == []
    with pytest.raises(SystemExit) as raised:
        host.raise_held(box)
    assert raised.value.code == 3


def test_a_held_call_gets_the_swarm_command_module_not_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    monkeypatch.setattr(host, "import_module", lambda name: f"module {name}")
    state = {"box": {}, "slug": "sw"}
    assert host.held(lambda cli, get: seen.append((cli, get("slug"))) or ["done"])("tick", state.get) == ["done"]
    assert seen == [("module scripts.swarm.cli", "sw")]


def test_a_held_call_after_a_held_exception_does_not_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    monkeypatch.setattr(host, "import_module", lambda name: "cli")
    box: dict[str, Any] = {"raised": RuntimeError("earlier")}
    assert host.held(lambda cli, get: seen.append(cli) or ["done"])("tick", {"box": box}.get) == []
    assert seen == []


def test_an_empty_box_raises_nothing() -> None:
    assert host.raise_held({}) is None


def test_a_viewed_step_without_a_view_skips_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    ran: list[object] = []
    step = host.viewed("stall watch", lambda cli, get: ran.append(cli) or ["ran"])
    line = "skipped stall watch: no step stored the tick view this pass"
    assert step("cli", {"view": None}.get) == [line]
    assert ran == []
    assert capsys.readouterr().err == f"{line}\n"


def test_a_viewed_step_with_a_view_runs_its_call(capsys: pytest.CaptureFixture[str]) -> None:
    step = host.viewed("stall watch", lambda cli, get: [cli, get("view")])
    assert step("cli", {"view": "lookup"}.get) == ["cli", "lookup"]
    assert capsys.readouterr().err == ""


def test_a_failed_pass_logs_the_error_and_records_the_pass_as_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    record: dict[str, Any] = {"outcome": "success"}
    token = timing.PASS.set(record)
    try:
        assert host.failed_pass(RuntimeError("boom")) == ["pass failed: a step raised RuntimeError: boom"]
    finally:
        timing.PASS.reset(token)
    assert record == {"outcome": "error", "error": "RuntimeError: boom"}
    assert capsys.readouterr().err.endswith("RuntimeError: boom\n")


def test_a_failed_pass_outside_a_pass_record_still_returns_its_line() -> None:
    assert host.failed_pass(ValueError("x")) == ["pass failed: a step raised ValueError: x"]


def test_a_held_failure_ends_the_pass_and_an_unheld_one_raises_again() -> None:
    held, other = RuntimeError("held"), RuntimeError("other")
    assert host.held_failure(held, {}, {"raised": held}) == ["pass failed: a step raised RuntimeError: held"]
    with pytest.raises(RuntimeError) as raised:
        host.held_failure(other, {"raised": held})
    assert raised.value is other


def test_a_held_redis_error_raises_again_for_the_outage_guard() -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    dropped = RedisConnectionError("dropped")
    with pytest.raises(RedisConnectionError) as raised:
        host.held_failure(dropped, {"raised": dropped})
    assert raised.value is dropped
    with pytest.raises(RedisConnectionError):
        host.end_held({"raised": dropped})


def test_end_held_returns_nothing_raises_a_stop_and_fails_an_error() -> None:
    assert host.end_held({}) == []
    with pytest.raises(SystemExit):
        host.end_held({"raised": SystemExit(2)})
    assert host.end_held({"raised": KeyError("k")}) == ["pass failed: a step raised KeyError: 'k'"]


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_cleartext_refusal_fails_the_pass_held_or_not_and_in_the_closing_group() -> None:
    from scripts.swarm_ledger.ledger_link import Cleartext

    refused = Cleartext("a ledger credential off loopback travels only over https")
    line = ["pass failed: a step raised Cleartext: a ledger credential off loopback travels only over https"]
    assert host.held_failure(refused) == line
    assert host.held_failure(refused, {"raised": refused}) == line
    assert host.end_held({"raised": refused}) == line
