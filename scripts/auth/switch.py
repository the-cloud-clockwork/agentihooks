"""The legacy credential switch, kept free of the token library so hive modules can read it in any environment."""

from collections.abc import Mapping

LEGACY_ENV = "AGENTIHOOKS_AUTH_LEGACY"
HOLDERS_ENV = "AGENTIHOOKS_HIVE_HOLDERS"
ENFORCE = "enforce"


def holders_enforced(environ: Mapping[str, str]) -> bool:
    """Whether hive agent token issue refuses a member with no holder record instead of backfilling and logging it."""
    return environ.get(HOLDERS_ENV) == ENFORCE


def legacy(environ: Mapping[str, str]) -> bool:
    """Whether services still accept today's credentials beside signed tokens."""
    return environ.get(LEGACY_ENV) != "off"
