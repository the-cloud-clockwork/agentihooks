"""The legacy credential switch, kept free of the token library so hive modules can read it in any environment."""

from collections.abc import Mapping

LEGACY_ENV = "AGENTIHOOKS_AUTH_LEGACY"


def legacy(environ: Mapping[str, str]) -> bool:
    """Whether services still accept today's credentials beside signed tokens."""
    return environ.get(LEGACY_ENV) != "off"
