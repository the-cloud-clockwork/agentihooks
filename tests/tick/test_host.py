"""The host steps run the tick wrapper's relay, probes, health, commands, refresh, intent and reboot calls in today's order."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.inbox import relay
from scripts.swarm import command_runner, incidents, ledger_probe, ledger_watchdog, outage, reboot, refresh_job, timing
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

    return SimpleNamespace(now_ms=lambda: 7, skip_refused=skip_refused, _intent_pass=intent_pass)


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
    found = actions(engine.run_group(group, Context(_State(state), Deadline.after(60.0))))
    assert set(loaded) <= {"scripts.swarm.cli"}
    return found


def test_the_host_steps_sit_at_their_sites_in_legacy_order() -> None:
    assert (host.PROBES, host.HEALTH, host.COMMANDS) == ("wrapper probes", "wrapper health", "wrapper commands")
    assert (host.REFRESH, host.INTENT, host.REBOOT) == ("wrapper refresh", "wrapper intent", "wrapper reboot")
    assert (host.RAISED, host.JUDGED) == ("raised", "judged")
    assert [(step.name, step.group, step.order) for step in host.STEPS] == [
        ("inbox relay", "wrapper probes", 10),
        ("launch slots", "wrapper probes", 20),
        ("outage probes", "wrapper probes", 30),
        ("host pressure", "wrapper health", 10),
        ("ledger watchdog", "wrapper health", 20),
        ("ledger probe", "wrapper health", 30),
        ("command runner", "wrapper commands", 10),
        ("refresh job", "wrapper refresh", 10),
        ("intent check", "wrapper intent", 10),
        ("reboot pass", "wrapper reboot", 10),
    ]
    registered = [step for wave in registry.waves() for step in wave]
    for step in host.STEPS:
        assert step in registered
        assert (step.lane, step.budget_s) == (CallStep.lane, CallStep.budget_s)


def test_the_probes_relay_the_inbox_refresh_slots_and_probe_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run(host.PROBES, calls, _pass(), monkeypatch) == []
    assert calls == [
        ("call", relay.relay, "redis"),
        ("call", ("refresh reported", "redis"), 7),
        ("providers", STORE, "sw", "runtime", 7),
    ]


def test_the_health_steps_return_pressure_watchdog_and_probe_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run(host.HEALTH, calls, _pass(), monkeypatch) == ["ran 1", "ran 2", "ran 3"]
    assert calls == [
        ("skip", incidents.host_pressure, STORE, "sw"),
        ("call", ledger_watchdog.watch, STORE, "sw", "ledger", "runtime"),
        ("call", ledger_probe.observe, STORE, "sw", "ledger", "runtime", 7),
    ]


def test_the_command_runner_consumes_the_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run(host.COMMANDS, calls, _pass(), monkeypatch) == ["ran 1"]
    assert calls == [("call", command_runner.consume, STORE, "sw")]


def test_the_refresh_job_starts_only_on_a_scheduled_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run(host.REFRESH, calls, _pass(scheduled=False), monkeypatch) == []
    assert calls == []
    assert _run(host.REFRESH, calls, _pass(scheduled=True), monkeypatch) == []
    assert calls == [("call", refresh_job.start, STORE, "sw")]


def test_the_intent_check_keeps_its_judgement_in_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    state = _pass(box, inbox="inbox", doc="doc", config="config")
    assert _run(host.INTENT, calls, state, monkeypatch) == []
    assert calls == [("intent", "inbox", STORE, "sw", "doc", "ledger", "config")]
    assert box == {host.JUDGED: ("view", "checked")}


def test_the_reboot_pass_boots_with_the_fenced_pass_state(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    assert _run(host.REBOOT, calls, _pass(), monkeypatch) == ["ran 1"]
    assert calls == [("skip", reboot.boot_pass, "sw", STORE, "ledger", "runtime", 7)]


def test_a_raising_step_skips_the_rest_and_raises_at_the_site(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    box: dict[str, Any] = {}
    state = _pass(box, raises=incidents.host_pressure)
    assert _run(host.HEALTH, calls, state, monkeypatch) == []
    assert calls == [("skip", incidents.host_pressure, STORE, "sw")]
    assert list(box) == [host.RAISED]
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
    assert host.held(lambda cli, get: seen.append(cli) or ["done"])("tick", {"box": {}}.get) == ["done"]
    assert seen == ["module scripts.swarm.cli"]


def test_an_empty_box_raises_nothing() -> None:
    assert host.raise_held({}) is None
