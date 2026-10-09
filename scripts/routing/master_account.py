import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hooks.context.account_sessions import CODEX_DEFAULT, CODEX_TOKEN_PREFIX, TOKEN_PREFIX
from scripts.routing.settings import SettingsStore
from scripts.routing.slots import INTERACTIVE, SUBSCRIPTION

if TYPE_CHECKING:
    from scripts.claude_quota_balancer import RowMarks

HARNESSES = ("claude", "codex")


@dataclass(frozen=True)
class MasterAccount:
    harness: str
    slug: str
    tier: str = ""
    kind: str = SUBSCRIPTION

    @property
    def marker(self) -> str:
        return f"MASTER {self.tier}".rstrip()


def _kind(harness: str, slug: str, environ: Mapping[str, str]) -> str:
    if harness == "claude":
        return SUBSCRIPTION if environ.get(f"{TOKEN_PREFIX}{slug}") else INTERACTIVE
    return INTERACTIVE if slug == CODEX_DEFAULT else SUBSCRIPTION


def check(harness: str, slug: str, environ: Mapping[str, str]) -> None:
    if harness == "codex" and slug != CODEX_DEFAULT and not environ.get(f"{CODEX_TOKEN_PREFIX}{slug}"):
        raise ValueError(f"unknown codex token slug {slug}: no {CODEX_TOKEN_PREFIX}{slug} is set")


def declare(
    store: SettingsStore,
    declarations: Mapping[str, tuple[str, str | None]],
    environ: Mapping[str, str],
    actor: str,
    now: float,
) -> None:
    for harness, (slug, _) in declarations.items():
        check(harness, slug, environ)
    for harness, (slug, tier) in declarations.items():
        store.set(f"master-account-{harness}", slug, actor, now)
        store.set(f"master-tier-{harness}", tier, actor, now)


def clear(store: SettingsStore, harnesses: Iterable[str], actor: str, now: float) -> None:
    for harness in harnesses:
        store.set(f"master-account-{harness}", None, actor, now)
        store.set(f"master-tier-{harness}", None, actor, now)


def declared(store: SettingsStore, environ: Mapping[str, str]) -> dict[str, MasterAccount]:
    values = store.all()
    return {
        harness: MasterAccount(harness, slug, values.get(f"master-tier-{harness}") or "", _kind(harness, slug, environ))
        for harness in HARNESSES
        if (slug := values.get(f"master-account-{harness}"))
    }


def load(environ: Mapping[str, str]) -> dict[str, MasterAccount]:
    from scripts.routing import place
    from scripts.routing.settings import open_store

    return declared(open_store(place._client(environ), environ), environ)


def row_marks(current: str = "", environ: Mapping[str, str] = os.environ) -> "RowMarks":
    from scripts.claude_quota_balancer import RowMarks

    return RowMarks(current, load(environ).get("claude"))
