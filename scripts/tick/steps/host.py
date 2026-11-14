"""The host steps of the tick wrapper: relay, launch slots, probes, host health, commands, phases, refresh, intent and reboot."""

import sys
import traceback
from collections.abc import Callable
from concurrent.futures import Future, wait
from importlib import import_module
from itertools import count
from threading import Lock
from typing import Any, cast

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
from scripts.tick import pool, registry
from scripts.tick import view as ledger_view
from scripts.tick.engine import SLOW
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, Left, hook_domains, pass_domains
from scripts.tick.steps.tasks import PHASE_CHANGED, PHASES, WriteStep

Get = Callable[[str], Any]
JUDGING = "judging"
REDIS_ONLY = frozenset({"host pressure", "prompt backlog", "hive sweep", "outbox migration"})
VIEW_RESERVE_S = 20.0
PENDING_VIEW = object()
_LAST_VIEWS: dict[str, Any] = {}
_VIEW_ORDER: dict[str, int] = {}
_BUILDS = count()
_KEEPING = Lock()


def held(call: Callable[[Any, Get], Any], box: str = "box") -> Callable[[Any, Get], Any]:
    """Run ``call`` with the swarm command module, skipping once a step held an exception in the pass value ``box``
    and holding the one it raises there."""

    def run(tick: Any, get: Get) -> Any:
        found = get(box)
        if "raised" in found:
            return []
        try:
            return call(import_module("scripts.swarm.cli"), get)
        except (Left, ledger_client.LedgerOffline):
            return []
        except (Exception, SystemExit) as exc:
            found["raised"] = exc
            return []

    return run


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written wrapper let it escape."""
    if "raised" in box:
        raise box["raised"]


def viewed(name: str, call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Run ``call`` only when the pass holds a tick view, else skip step ``name`` this pass and log that it skipped."""

    def run(cli: Any, get: Get) -> Any:
        if get("view") is None:
            line = f"skipped {name}: no step stored the tick view this pass"
            print(line, file=sys.stderr)
            return [line]
        return call(cli, get)

    return run


def held_failure(exc: BaseException, *boxes: dict[str, Any]) -> list[str]:
    """Return the line ending the pass as failed when ``exc`` is an exception a step held in one of ``boxes`` or a
    ledger address refusing the credential over plain http, else raise it again."""
    from redis.exceptions import RedisError

    from scripts.swarm_ledger.ledger_link import Cleartext

    if isinstance(exc, Cleartext):
        return failed_pass(exc)
    if isinstance(exc, RedisError) or not any(box.get("raised") is exc for box in boxes):
        raise exc
    return failed_pass(exc)


def failed_pass(exc: BaseException) -> list[str]:
    """Log the exception a step held, record its pass as an error and return the line ending the pass as failed."""
    traceback.print_exception(exc, file=sys.stderr)
    record = timing.PASS.get()
    if record is not None:
        timing.failed(record, exc)
    return [f"pass failed: a step raised {type(exc).__name__}: {exc}"]


def end_held(box: dict[str, Any]) -> list[str]:
    """Raise a stop a step held in ``box``, else log the exception it held, a ledger address refusing the credential
    over plain http included, and return the line ending its pass as failed, nothing when no step raised."""
    from redis.exceptions import RedisError

    from scripts.swarm_ledger.ledger_link import Cleartext

    raised = box.get("raised")
    if raised is None:
        return []
    if not isinstance(raised, (Exception, Cleartext)) or isinstance(raised, RedisError):
        raise raised
    return failed_pass(raised)


def _inbox_relay(cli: Any, get: Get) -> None:
    timing.call(cli.relay.relay, get("store").redis)  # pyright: ignore[reportUnknownMemberType]


def _launch_slots(cli: Any, get: Get) -> None:
    timing.call(LaunchSlots(get("store").redis).refresh_reported, cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _outage_probes(cli: Any, get: Get) -> None:
    outage.providers(get("store"), get("slug"), get("runtime"), cli.now_ms())
    outage.share(get("store"), get("slug"))


def _hive_ledger(cli: Any, get: Get) -> None:
    hive_ledger.observe(get("store"), get("slug"), cli.now_ms())


def _ledger_health(cli: Any, get: Get) -> None:
    ledger, slug = get("ledger"), get("slug")
    if (
        isinstance(ledger, ledger_client.LedgerClient)
        and not outage.ledger_health(
            slug,
            cli.now_ms(),
            lambda: isinstance(ledger.metadata(slug), dict),  # pyright: ignore[reportUnknownMemberType]
        )
        and ledger_view.stored(slug) is None
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


def _dependency_audit(cli: Any, get: Get) -> None:
    from scripts.swarm.health import ordering_dependencies

    timing.call(ordering_dependencies.observe, get("store"), get("slug"), get("doc"))  # pyright: ignore[reportUnknownMemberType]


def _phase_passes(cli: Any, get: Get) -> Any:
    return cli._phase_steps(get("inbox"), get("store"), get("slug"), get("doc"), get("ledger"))


def _planning_after_phase(cli: Any, get: Get) -> Any:
    return cli._planning_after_phase(get("inbox"), get("store"), get("slug"), get("ledger"), get("box"))


def _intent_check(cli: Any, get: Get) -> None:
    judging = get(JUDGING)
    judging["judged"] = cli._intent_pass(
        get("inbox"), get("store"), get("slug"), judging["doc"], get("ledger"), judging["config"]
    )


def _reboot_pass(cli: Any, get: Get) -> Any:
    return cli.skip_refused(reboot.boot_pass, get("slug"), get("store"), get("ledger"), get("runtime"), cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


def _outbox_migration(cli: Any, get: Get) -> None:
    timing.call(delivery.migrate_outbox, get("store"), get("slug"), get("inbox"))  # pyright: ignore[reportUnknownMemberType]


def listed(store: Any, slug: str) -> list[Any]:
    """Return the agents of swarm ``slug`` that have not finished, as the page relay stores them for the pass."""
    return [a for a in timing.call(store.agents, slug) if a.state != "finished"]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def _page_relay(cli: Any, get: Get) -> None:
    agents = listed(get("store"), get("slug"))
    get("box")["agents"] = agents
    cli.skip_refused(delivery.relay_to_page, get("inbox"), get("slug"), agents, get("ledger"))  # pyright: ignore[reportUnknownMemberType]


def _tick_view(cli: Any, get: Get) -> None:
    earlier = get("view")
    if earlier is PENDING_VIEW:
        get("box")["view"] = None
        return
    view = timing.call(ledger_events.tick_view, get("inbox"), get("store"), get("slug"), get("doc"), earlier)  # pyright: ignore[reportUnknownMemberType]
    get("box")["view"] = view


def pass_view(slug: str, view: Any) -> Any:
    """Return the pass view of swarm ``slug``: ``view`` itself unless it is a build, the build's view or error when it
    ends while both lanes keep ``VIEW_RESERVE_S``, else the last view the swarm completed in this process, or
    ``PENDING_VIEW`` when it completed none yet; a late build replaces that last view once it ends."""
    if not isinstance(view, Future):
        return view
    build = cast(Future[Any], view)
    earlier = _LAST_VIEWS.get(slug)
    order = next(_BUILDS)
    build.add_done_callback(lambda built: _keep_view(slug, built, order))
    wait((build,), timeout=_view_wait_s())
    if build.done():
        return build.result()
    return PENDING_VIEW if earlier is None else earlier


def settled_view(view: Any) -> Any:
    """Return ``view``, or None while it is still ``PENDING_VIEW``."""
    return None if view is PENDING_VIEW else view


def _keep_view(slug: str, built: Future[Any], order: int) -> None:
    """Keep the view ``built`` made as the last view of swarm ``slug`` when the build ended without an error and no
    build the swarm started after it, by ``order``, is kept already."""
    if built.cancelled() or built.exception() is not None:
        return
    with _KEEPING:
        if _VIEW_ORDER.get(slug, order) <= order:
            _LAST_VIEWS[slug], _VIEW_ORDER[slug] = built.result(), order


def _view_wait_s() -> float | None:
    """Return the seconds the pass may wait for its view: the time left in the earlier ending of the critical and
    slow lanes less ``VIEW_RESERVE_S``, or no bound outside a pass."""
    current = pool.active()
    if current is None:
        return None
    return max(0.0, min(current.critical.remaining, current.slow.remaining) - VIEW_RESERVE_S)


STEPS = (
    CallStep("inbox relay", "wrapper probes", held(_inbox_relay)),
    CallStep("launch slots", "wrapper probes", held(_launch_slots)),
    CallStep("outage probes", "wrapper probes", held(_outage_probes)),
    CallStep("hive ledger", "wrapper probes", held(_hive_ledger), lane=SLOW),
    CallStep("ledger health", "wrapper probes", held(_ledger_health), writes=pass_domains("stop")),
    CallStep("host pressure", "wrapper health", held(_host_pressure), lane=SLOW),
    CallStep("ledger watchdog", "wrapper health", held(_ledger_watchdog), lane=SLOW),
    CallStep("ledger probe", "wrapper health", held(_ledger_probe), lane=SLOW),
    CallStep("prompt backlog", "wrapper health", held(_prompt_backlog), lane=SLOW),
    CallStep("command runner", "wrapper commands", held(_command_runner)),
    CallStep("urgent hold", "wrapper phases", held(_urgent_hold), reads=hook_domains("doc")),
    CallStep(
        "dependency audit",
        "wrapper phases",
        held(_dependency_audit),
        needs=frozenset(),
        reads=hook_domains("doc"),
        writes=frozenset(),
        lane=CallStep.lane,
        effects=frozenset(),
        feature="swarm-ordering-dependency-audit",
    ),
    WriteStep(
        "planning and phase pass",
        "wrapper phases",
        held(_phase_passes),
        reads=hook_domains("doc"),
        writes=frozenset({PHASES}) | pass_domains(PHASE_CHANGED),
    ),
    CallStep(
        "planning after phase",
        "wrapper phases",
        held(_planning_after_phase),
        frozenset({"planning and phase pass"}),
        pass_domains(PHASE_CHANGED),
    ),
    CallStep("refresh job", "wrapper refresh", held(_refresh_job)),
    CallStep(
        "intent check",
        "wrapper intent",
        held(_intent_check, JUDGING),
        reads=hook_domains("doc", "config"),
        writes=pass_domains("judged"),
    ),
    CallStep("reboot pass", "wrapper reboot", held(_reboot_pass)),
    CallStep("outbox migration", "wrapper relay", held(_outbox_migration)),
    CallStep("page relay", "wrapper relay", held(_page_relay), writes=pass_domains("agents")),
    CallStep(
        "tick view", "wrapper view", held(_tick_view), reads=hook_domains("doc"), writes=pass_domains("view"), kept=True
    ),
)

for _step in STEPS:
    registry.register(_step)


def _hive_sweep(cli: Any, get: Get) -> Any:
    """Drain and remove each ephemeral hive quiet past the registry window, returning what it removed."""
    from scripts.hive import sweep

    return timing.call(sweep.sweep, get("store"), cli.now_ms())  # pyright: ignore[reportUnknownMemberType]


registry.register(
    CallStep(
        "hive sweep",
        "wrapper health",
        held(_hive_sweep),
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"coordination"}),
        feature="hive-quiet-sweep",
    )
)
