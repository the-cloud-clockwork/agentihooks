from contextlib import ExitStack
from typing import Any

from scripts.tick import pool


def _hold_view(beating: ExitStack, slug: str, ledger: Any) -> None:
    del beating, slug, ledger


def held_inside_the_lane(beating: ExitStack, slug: str, ledger: Any, stopped: bool) -> None:
    beating.enter_context(pool.running())
    if stopped:
        # ruleid: tick-pass-holds-view-inside-lane
        _hold_view(beating, slug, ledger)
        return
    # ruleid: tick-pass-holds-view-inside-lane
    _hold_view(beating, slug, ledger)


def held_before_the_lane(beating: ExitStack, slug: str, ledger: Any) -> None:
    # ok: tick-pass-holds-view-inside-lane
    _hold_view(beating, slug, ledger)
    beating.enter_context(pool.running())
