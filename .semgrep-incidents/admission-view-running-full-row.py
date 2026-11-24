from typing import Any


def running_rows(ledger: Any, slug: str) -> list[dict[str, Any]]:
    # ruleid: admission-view-running-full-row
    return ledger.tasks_where(slug, states=("claimed", "pr"))


def pinned_rows(ledger: Any, slug: str) -> list[dict[str, Any]]:
    # ruleid: admission-view-running-full-row
    return ledger.tasks_where(slug, states=("open",), pinned=True)


def related_rows(ledger: Any, slug: str, wanted: list[str]) -> list[dict[str, Any]]:
    # ruleid: admission-view-running-full-row
    return ledger.tasks_where(slug, ids=wanted)


def narrow_running_rows(ledger: Any, slug: str) -> list[dict[str, Any]]:
    # ok: admission-view-running-full-row
    return ledger.tasks_where(slug, states=("claimed", "pr"), fields=("state", "territory"))


def narrow_related_rows(ledger: Any, slug: str, wanted: list[str]) -> list[dict[str, Any]]:
    # ok: admission-view-running-full-row
    return ledger.tasks_where(slug, ids=wanted, fields=("state",))
