"""The report steps of the tick wrapper: ledger events, watches, findings, metrics, wake, snapshot, heartbeat and status."""

import os
from collections.abc import Callable
from typing import Any

from scripts.gates import progress, quiet
from scripts.swarm import (
    bottleneck_status,
    dispatcher,
    doc_report,
    done_gate,
    ledger_events,
    metrics,
    overrun,
    ready,
    reaper,
    release_wait,
    stall_watch,
    tick_heartbeat,
    timing,
    waits,
)
from scripts.swarm.status import findings  # pyright: ignore[reportUnknownVariableType]
from scripts.tick import registry
from scripts.tick.steps import CallStep
from scripts.tick.steps.host import held

Get = Callable[[str], Any]


def _event_pass(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        ledger_events.event_pass,  # pyright: ignore[reportUnknownMemberType]
        get("inbox"),
        get("store"),
        get("slug"),
        get("doc"),
        get("ledger"),
        cli.now_ms(),
        get("view"),
    )


def _done_recheck(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        done_gate.recheck_pass,  # pyright: ignore[reportUnknownMemberType]
        get("store"),
        get("slug"),
        get("doc"),
        get("ledger"),
        cli.now_ms(),
        get("view"),
    )


def _progress_checks(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        progress.checks_pass,  # pyright: ignore[reportUnknownMemberType]
        get("store").redis,
        get("slug"),
        get("doc")["tasks"],
        get("view"),
        cli.now_ms(),
    )


def _waits_end(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        waits.end_pass,  # pyright: ignore[reportUnknownMemberType]
        get("store"),
        get("slug"),
        get("rows"),
        get("inbox"),
        get("view"),
        cli.now_ms(),
        ledger_events.view,  # pyright: ignore[reportUnknownMemberType]
    )


def _merge_pass(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        ready.merge_pass,  # pyright: ignore[reportUnknownMemberType]
        ready.Pass(get("store"), get("slug"), get("ledger"), get("view"), ledger_events.view, cli.now_ms()),  # pyright: ignore[reportUnknownMemberType]
        get("rows"),
    )


def _stall_watch(cli: Any, get: Get) -> Any:
    store, slug, ledger, runtime = get("store"), get("slug"), get("ledger"), get("runtime")
    live = runtime.live_names() | {a.name for a in get("agents") if a.runtime_backend != "local"}
    get("box")["live"] = live
    return cli.skip_refused(
        stall_watch.stall_pass,  # pyright: ignore[reportUnknownMemberType]
        stall_watch.Watch(
            store,
            slug,
            ledger,
            get("view"),
            frozenset(live),
            lambda agent: runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
            stall_watch.ready_lanes(slug, store, ledger, get("doc"), cli.now_ms()),
            cli.now_ms(),
            released=release_wait.checker(get("config").repo),  # pyright: ignore[reportUnknownMemberType]
            reread=ledger_events.view,  # pyright: ignore[reportUnknownMemberType]
        ),
        get("doc"),
    )


def _overrun(cli: Any, get: Get) -> Any:
    slug, runtime = get("slug"), get("runtime")
    return cli.skip_refused(
        overrun.overrun_pass,  # pyright: ignore[reportUnknownMemberType]
        overrun.Watch(
            get("store"),
            slug,
            get("ledger"),
            frozenset(get("box")["live"]),
            lambda agent: runtime.retire(agent, homes=reaper.scratch_homes(slug, agent.task)),  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
            cli.now_ms(),
        ),
        get("doc"),
    )


def _quiet(cli: Any, get: Get) -> Any:
    return cli.skip_refused(quiet.quiet_pass, get("store"), get("slug"), get("rows"), cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _doc_report(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        doc_report.report_pass,  # pyright: ignore[reportUnknownMemberType]
        get("inbox"),
        get("ledger"),
        get("slug"),
        get("doc")["tasks"],
        cli.now_ms(),
        get("config").watchdog_address,
    )


def _priorities(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        dispatcher.priorities,  # pyright: ignore[reportUnknownMemberType]
        get("store"),
        get("slug"),
        get("doc"),
        get("ledger"),
        get("view"),
        cli.now_ms(),
    )


def _health_findings(cli: Any, get: Get) -> None:
    doc = get("doc")
    get("box")["found"] = timing.call(  # pyright: ignore[reportUnknownMemberType]
        findings,
        get("store"),
        get("slug"),
        get("config"),
        doc.get("tasks", []),
        doc.get("_meta", {}).get("events", []),
        get("view"),
    )


def _findings_pass(cli: Any, get: Get) -> Any:
    return cli.skip_refused(
        ledger_events.findings_pass,  # pyright: ignore[reportUnknownMemberType]
        get("inbox"),
        get("store"),
        get("slug"),
        get("box")["found"],
        get("doc"),
    )


def _metrics(cli: Any, get: Get) -> Any:
    return timing.call(  # pyright: ignore[reportUnknownMemberType]
        metrics.record_pass,
        get("slug"),
        cli.now_ms(),
        get("count"),
        os.environ,
        metrics.metrics_swarm.TickInput(get("store"), get("doc"), get("box")["found"], get("view"), get("ledger")),
    )


def _bottleneck_status(cli: Any, get: Get) -> Any:
    return cli.skip_refused(bottleneck_status.refresh, get("slug"), get("store"), cli.now_ms())


def _inbox_wake(cli: Any, get: Get) -> Any:
    window = cli.wake.window_ms(os.environ)  # pyright: ignore[reportUnknownMemberType]
    return cli.skip_refused(cli.wake.maintenance, get("inbox"), get("slug"), get("agents"), cli.now_ms(), window)  # pyright: ignore[reportUnknownMemberType]


def _snapshot_job(cli: Any, get: Get) -> None:
    get("box")["started"] = timing.call(cli.snapshot_job.start, get("store"), get("slug"), cli.now_ms(), os.environ)  # pyright: ignore[reportUnknownMemberType]


def _last_tick(cli: Any, get: Get) -> None:
    store = get("store")
    store.redis.set(store.key(get("slug"), "last-tick"), cli.now_ms())


def _heartbeat(cli: Any, get: Get) -> None:
    tick_heartbeat.write(get("store"), get("slug"), cli.now_ms(), get("doc"))  # pyright: ignore[reportUnknownMemberType]


def _status_publish(cli: Any, get: Get) -> None:
    cli._publish_tick_status(get("store"), get("slug"), get("doc"), get("view"))


STEPS = (
    CallStep("ledger event pass", "wrapper events", 10, held(_event_pass)),
    CallStep("done recheck", "wrapper events", 20, held(_done_recheck)),
    CallStep("progress checks", "wrapper events", 30, held(_progress_checks)),
    CallStep("waits end", "wrapper events", 40, held(_waits_end)),
    CallStep("merge pass", "wrapper events", 50, held(_merge_pass)),
    CallStep("stall watch", "wrapper events", 60, held(_stall_watch)),
    CallStep("overrun pass", "wrapper events", 70, held(_overrun), frozenset({"stall watch"})),
    CallStep("quiet pass", "wrapper events", 80, held(_quiet)),
    CallStep("doc report", "wrapper events", 90, held(_doc_report)),
    CallStep("priorities sweep", "wrapper events", 100, held(_priorities)),
    CallStep("health findings", "wrapper events", 110, held(_health_findings)),
    CallStep("findings pass", "wrapper events", 120, held(_findings_pass), frozenset({"health findings"})),
    CallStep("metrics record", "wrapper metrics", 10, held(_metrics), frozenset({"health findings"})),
    CallStep("bottleneck status", "wrapper metrics", 20, held(_bottleneck_status)),
    CallStep("inbox wake", "wrapper metrics", 30, held(_inbox_wake)),
    CallStep("snapshot job", "wrapper metrics", 40, held(_snapshot_job)),
    CallStep("last tick", "wrapper metrics", 50, held(_last_tick)),
    CallStep("heartbeat", "after", 10, held(_heartbeat)),
    CallStep("status publish", "wrapper status", 10, held(_status_publish)),
)

for _step in STEPS:
    registry.register(_step)
