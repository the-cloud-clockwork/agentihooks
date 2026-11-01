"""The host steps of the tick wrapper: relay, launch slots, probes, host health, commands, refresh, intent and reboot."""

from collections.abc import Callable
from importlib import import_module
from typing import Any

from scripts.inbox import relay
from scripts.routing.reservation import LaunchSlots
from scripts.swarm import ledger_probe, ledger_watchdog, outage, reboot, timing
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
    timing.call(relay.relay, get("store").redis)


def _launch_slots(cli: Any, get: Get) -> None:
    timing.call(LaunchSlots(get("store").redis).refresh_reported, cli.now_ms())


def _outage_probes(cli: Any, get: Get) -> None:
    outage.providers(get("store"), get("slug"), get("runtime"), cli.now_ms())


def _host_pressure(cli: Any, get: Get) -> Any:
    from scripts.swarm import incidents

    return cli.skip_refused(incidents.host_pressure, get("store"), get("slug"))


def _ledger_watchdog(cli: Any, get: Get) -> Any:
    return timing.call(ledger_watchdog.watch, get("store"), get("slug"), get("ledger"), get("runtime"))


def _ledger_probe(cli: Any, get: Get) -> Any:
    return timing.call(ledger_probe.observe, get("store"), get("slug"), get("ledger"), get("runtime"), cli.now_ms())


def _command_runner(cli: Any, get: Get) -> Any:
    from scripts.swarm import command_runner

    return timing.call(command_runner.consume, get("store"), get("slug"))


def _refresh_job(cli: Any, get: Get) -> None:
    if get("scheduled"):
        from scripts.swarm import refresh_job

        timing.call(refresh_job.start, get("store"), get("slug"))


def _intent_check(cli: Any, get: Get) -> None:
    get("box")["judged"] = cli._intent_pass(
        get("inbox"), get("store"), get("slug"), get("doc"), get("ledger"), get("config")
    )


def _reboot_pass(cli: Any, get: Get) -> Any:
    return cli.skip_refused(reboot.boot_pass, get("slug"), get("store"), get("ledger"), get("runtime"), cli.now_ms())


STEPS = (
    CallStep("inbox relay", "wrapper probes", 10, held(_inbox_relay)),
    CallStep("launch slots", "wrapper probes", 20, held(_launch_slots)),
    CallStep("outage probes", "wrapper probes", 30, held(_outage_probes)),
    CallStep("host pressure", "wrapper health", 10, held(_host_pressure)),
    CallStep("ledger watchdog", "wrapper health", 20, held(_ledger_watchdog)),
    CallStep("ledger probe", "wrapper health", 30, held(_ledger_probe)),
    CallStep("command runner", "wrapper commands", 10, held(_command_runner)),
    CallStep("refresh job", "wrapper refresh", 10, held(_refresh_job)),
    CallStep("intent check", "wrapper intent", 10, held(_intent_check)),
    CallStep("reboot pass", "wrapper reboot", 10, held(_reboot_pass)),
)

for _step in STEPS:
    registry.register(_step)
