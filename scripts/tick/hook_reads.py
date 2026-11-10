"""The runtime guard on the pass box reads of tick and wrapper site hooks: each hook declares the keys it reads, and
a read outside them logs in observe mode, the default, or raises ``UndeclaredHookRead`` in enforce mode."""

import logging
import os
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass

ENV = "AGENTIHOOKS_TICK_HOOK_READS"
OBSERVE = "observe"
ENFORCE = "enforce"
OFF = "off"
MODES = (OBSERVE, ENFORCE, OFF)
LOG = logging.getLogger(__name__)


class UndeclaredHookRead(RuntimeError):
    """A site hook read a pass box key it does not declare."""


@dataclass(frozen=True)
class Scope:
    """The site whose hook is running on this thread and the pass box keys it declares."""

    site: str
    reads: frozenset[str]
    mode: str


HOOK: ContextVar[Scope | None] = ContextVar("tick site hook", default=None)


def mode(environ: Mapping[str, str]) -> str:
    """Return the guard mode ``environ`` names, observe when it names none or an unknown one."""
    chosen = environ.get(ENV, OBSERVE).strip().lower()
    return chosen if chosen in MODES else OBSERVE


def seen(key: object) -> None:
    """Check a pass box read of ``key`` against the site hook running on this thread, if any."""
    scope = HOOK.get()
    if scope is None or key in scope.reads:
        return
    line = f"site hook {scope.site} read pass box {key} without declaring it"
    if scope.mode == ENFORCE:
        raise UndeclaredHookRead(line)
    LOG.warning(line)


def _scoped(scope: Scope, hook: Callable[[], bool]) -> Callable[[], bool]:
    """Return ``hook`` running with ``scope`` as this thread's site hook."""

    def run() -> bool:
        token = HOOK.set(scope)
        try:
            return hook()
        finally:
            HOOK.reset(token)

    return run


def guarded(
    hooks: Mapping[str, Callable[[], bool]],
    reads: Mapping[str, frozenset[str]],
    environ: Mapping[str, str] = os.environ,
) -> dict[str, Callable[[], bool]]:
    """Return ``hooks`` each running under the guard on the keys ``reads`` declares for its site, none when it
    declares nothing; unchanged when the guard is off."""
    chosen = mode(environ)
    if chosen == OFF:
        return dict(hooks)
    return {site: _scoped(Scope(site, reads.get(site, frozenset()), chosen), hook) for site, hook in hooks.items()}
