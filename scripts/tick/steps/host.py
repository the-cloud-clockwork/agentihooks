"""The host steps of the tick wrapper: relay, launch slots, probes, host health, commands, phases, refresh, intent and reboot."""

from collections.abc import Callable
from importlib import import_module
from typing import Any

from scripts.routing.reservation import LaunchSlots
from scripts.swarm import (
    delivery,
    hive_ledger,
    ledger_client,
    ledger_events,
    ledger_probe,
    ledger_watchdog,
    outage,
    reboot,
    timing,
)
from scripts.swarm.health import prompt_backlog
from scripts.tick import registry
from scripts.tick.steps import CallStep

Get = Callable[[str], Any]


def held(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Run ``call`` with the swarm command module, skipping once a step held an exception and holding the one it raises."""

    def run(tick: Any, get: Get) -> Any:
        box = get("box")
        if "raised" in box:
            return []
        try:
            return call(import_module("scripts.swarm.cli"), get)
        except (Exception, SystemExit) as exc:
            box["raised"] = exc
            return []

    return run


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written wrapper let it escape."""
    if "raised" in box:
        raise box["raised"]


def _inbox_relay(cli: Any, get: Get) -> None:
    timing.call(cli.relay.relay, get("store").redis)  # pyright: ignore[reportUnknownMemberType]


def _launch_slots(cli: Any, get: Get) -> None:
    timing.call(LaunchSlots(get("store").redis).refresh_reported, cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _outage_probes(cli: Any, get: Get) -> None:
    outage.providers(get("store"), get("slug"), get("runtime"), cli.now_ms())


def _hive_ledger(cli: Any, get: Get) -> None:
    hive_ledger.observe(get("store"), get("slug"), cli.now_ms())


def _ledger_health(cli: Any, get: Get) -> None:
    ledger, slug = get("ledger"), get("slug")
    if isinstance(ledger, ledger_client.LedgerClient) and not outage.ledger_health(
        slug,
        cli.now_ms(),
        lambda: isinstance(ledger.metadata(slug), dict),  # pyright: ignore[reportUnknownMemberType]
    ):
        get("box")["stop"] = ["Ledger health probe failed"]


def _host_pressure(cli: Any, get: Get) -> Any:
    from scripts.swarm import incidents

    return cli.skip_refused(incidents.host_pressure, get("store"), get("slug"))


def _ledger_watchdog(cli: Any, get: Get) -> Any:
    return timing.call(ledger_watchdog.watch, get("store"), get("slug"), get("ledger"), get("runtime"))  # pyright: ignore[reportUnknownMemberType]


def _ledger_probe(cli: Any, get: Get) -> Any:
    return timing.call(ledger_probe.observe, get("store"), get("slug"), get("ledger"), get("runtime"), cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _prompt_backlog(cli: Any, get: Get) -> None:
    timing.call(prompt_backlog.observe, get("store"), get("slug"), cli.now_ms() // 1000)  # pyright: ignore[reportUnknownMemberType]


def _command_runner(cli: Any, get: Get) -> Any:
    from scripts.swarm import command_runner

    return timing.call(command_runner.consume, get("store"), get("slug"))  # pyright: ignore[reportUnknownMemberType]


def _refresh_job(cli: Any, get: Get) -> None:
    if get("scheduled"):
        timing.call(cli.refresh_job.start, get("store"), get("slug"))  # pyright: ignore[reportUnknownMemberType]


def _urgent_hold(cli: Any, get: Get) -> None:
    from scripts.swarm.health import urgent_hold

    timing.call(urgent_hold.observe, get("store"), get("slug"), get("doc"), cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _phase_passes(cli: Any, get: Get) -> Any:
    return cli._phase_steps(get("inbox"), get("store"), get("slug"), get("doc"), get("ledger"))


def _intent_check(cli: Any, get: Get) -> None:
    get("box")["judged"] = cli._intent_pass(
        get("inbox"), get("store"), get("slug"), get("doc"), get("ledger"), get("config")
    )


def _reboot_pass(cli: Any, get: Get) -> Any:
    return cli.skip_refused(reboot.boot_pass, get("slug"), get("store"), get("ledger"), get("runtime"), cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _tick_call(cli: Any, get: Get) -> Any:
    runtime = get("runtime") or cli.routed(herdr=cli.HerdrRuntime())
    return timing.call(cli.tick, get("slug"), get("store"), get("ledger"), runtime, cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _outbox_migration(cli: Any, get: Get) -> None:
    timing.call(delivery.migrate_outbox, get("store"), get("slug"), get("inbox"))  # pyright: ignore[reportUnknownMemberType]


def _page_relay(cli: Any, get: Get) -> None:
    agents = [a for a in timing.call(get("store").agents, get("slug")) if a.state != "finished"]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    get("box")["agents"] = agents
    cli.skip_refused(delivery.relay_to_page, get("inbox"), get("slug"), agents, get("ledger"))  # pyright: ignore[reportUnknownMemberType]


def _tick_view(cli: Any, get: Get) -> None:
    view = timing.call(ledger_events.tick_view, get("inbox"), get("store"), get("slug"), get("doc"), get("view"))  # pyright: ignore[reportUnknownMemberType]
    get("box")["view"] = view


STEPS = (
    CallStep("inbox relay", "wrapper probes", 10, held(_inbox_relay)),
    CallStep("launch slots", "wrapper probes", 20, held(_launch_slots)),
    CallStep("outage probes", "wrapper probes", 30, held(_outage_probes)),
    CallStep("hive ledger", "wrapper probes", 40, held(_hive_ledger)),
    CallStep("ledger health", "wrapper probes", 50, held(_ledger_health)),
    CallStep("host pressure", "wrapper health", 10, held(_host_pressure)),
    CallStep("ledger watchdog", "wrapper health", 20, held(_ledger_watchdog)),
    CallStep("ledger probe", "wrapper health", 30, held(_ledger_probe)),
    CallStep("prompt backlog", "wrapper health", 40, held(_prompt_backlog)),
    CallStep("command runner", "wrapper commands", 10, held(_command_runner)),
    CallStep("urgent hold", "wrapper phases", 10, held(_urgent_hold)),
    CallStep("planning and phase pass", "wrapper phases", 20, held(_phase_passes)),
    CallStep("refresh job", "wrapper refresh", 10, held(_refresh_job)),
    CallStep("intent check", "wrapper intent", 10, held(_intent_check)),
    CallStep("reboot pass", "wrapper reboot", 10, held(_reboot_pass)),
    CallStep("tick call", "wrapper reboot", 20, held(_tick_call)),
    CallStep("outbox migration", "wrapper relay", 10, held(_outbox_migration)),
    CallStep("page relay", "wrapper relay", 20, held(_page_relay)),
    CallStep("tick view", "wrapper view", 10, held(_tick_view)),
)

for _step in STEPS:
    registry.register(_step)
