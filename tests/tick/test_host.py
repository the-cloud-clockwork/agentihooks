"""The host steps run the tick wrapper's relay, probes, health, commands, refresh, intent and reboot calls in today's order."""

from types import SimpleNamespace
from typing import Any

import pytest

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
from scripts.swarm.health import prompt_backlog, urgent_hold
from scripts.tick import engine, registry
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import PASS, CallStep, actions, host

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
TICK = object()


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
        return ["phased"]

    return SimpleNamespace(
        now_ms=lambda: 7,
        skip_refused=skip_refused,
        _intent_pass=intent_pass,
        _phase_steps=phase_steps,
        relay=relay,
        refresh_job=refresh_job,
        tick=TICK,
        routed=lambda herdr: ("routed", herdr),
        HerdrRuntime=lambda: "herdr",
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
    monkeypatch.setattr(hive_ledger, "observe", lambda *args: calls.append(("hive", *args)))
    found = actions(engine.run_group(group, Context(_State(state), Deadline.after(60.0))))
    assert set(loaded) <= {"scripts.swarm.cli"}
    return found


def test_the_host_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert [(step.name, step.group, step.order) for step in host.STEPS] == [
        ("inbox relay", "wrapper probes", 10),
        ("launch slots", "wrapper probes", 20),
        ("outage probes", "wrapper probes", 30),
        ("hive ledger", "wrapper probes", 40),
        ("ledger health", "wrapper probes", 50),
        ("host pressure", "wrapper health", 10),
        ("ledger watchdog", "wrapper health", 20),
        ("ledger probe", "wrapper health", 30),
        ("prompt backlog", "wrapper health", 40),
        ("command runner", "wrapper commands", 10),
        ("urgent hold", "wrapper phases", 10),
        ("phase steps", "wrapper phases", 20),
        ("refresh job", "wrapper refresh", 10),
        ("intent check", "wrapper intent", 10),
        ("reboot pass", "wrapper reboot", 10),
        ("tick call", "wrapper reboot", 20),
        ("outbox migration", "wrapper relay", 10),
        ("page relay", "wrapper relay", 20),
        ("tick view", "wrapper view", 10),
    ]
    registered = [step for wave in registry.waves() for step in wave]
    for step in host.STEPS:
        assert step in registered
        assert (step.lane, step.budget_s) == (CallStep.lane, CallStep.budget_s)


def test_the_probes_relay_the_inbox_refresh_slots_and_probe_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper probes", calls, _pass(), monkeypatch) == []
    assert calls == [
        ("call", relay.relay, "redis"),
        ("call", ("refresh reported", "redis"), 7),
        ("providers", STORE, "sw", "runtime", 7),
        ("hive", STORE, "sw", 7),
    ]


def test_the_health_steps_return_pressure_watchdog_and_probe_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper health", calls, _pass(), monkeypatch) == ["ran 1", "ran 2", "ran 3"]
    assert calls == [
        ("skip", incidents.host_pressure, STORE, "sw"),
        ("call", ledger_watchdog.watch, STORE, "sw", "ledger", "runtime"),
        ("call", ledger_probe.observe, STORE, "sw", "ledger", "runtime", 7),
        ("call", prompt_backlog.observe, STORE, "sw", 0),
    ]


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


def test_the_phase_steps_observe_the_urgent_hold_then_run_the_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper phases", calls, _pass(inbox="inbox", doc="doc"), monkeypatch) == ["phased"]
    assert calls == [
        ("call", urgent_hold.observe, STORE, "sw", "doc", 7),
        ("phases", "inbox", STORE, "sw", "doc", "ledger"),
    ]


def test_the_tick_call_runs_after_the_reboot_pass_with_the_pass_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run("wrapper reboot", calls, _pass(), monkeypatch) == ["ran 1", "ran 2"]
    assert calls[1] == ("call", TICK, "sw", STORE, "ledger", "runtime", 7)
    calls.clear()
    _run("wrapper reboot", calls, _pass(runtime=None), monkeypatch)
    assert calls[1] == ("call", TICK, "sw", STORE, "ledger", ("routed", "herdr"), 7)


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


def test_the_intent_check_keeps_its_judgement_in_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    state = _pass(box, inbox="inbox", doc="doc", config="config")
    assert _run("wrapper intent", calls, state, monkeypatch) == []
    assert calls == [("intent", "inbox", STORE, "sw", "doc", "ledger", "config")]
    assert box == {"judged": ("view", "checked")}


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
