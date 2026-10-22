"""agentihooks hive setup: the workstation agent settings and the bundle origin into the hive settings record."""

import ipaddress
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from scripts import openbao_kv
from scripts.hive import auth, bundle_origin, linked_profiles, registry
from scripts.hive.config import TOKEN_PREFIXES

NOT_SETTINGS = (auth.ENV_FILE, "hive-container.env")
TICK_ONLY = frozenset({"SWARM_SECRET", "AGENTIHOOKS_LEDGER_AGENT_TOKEN"})


def names(home: Path) -> list[str]:
    from hooks.context.credential_guard import dotenv_keys

    files = sorted(home.glob("*.env")) if home.is_dir() else []
    return sorted({name for path in files if path.name not in NOT_SETTINGS for name in dotenv_keys(path)})


def _local(value: str) -> bool:
    try:
        host = urlsplit(value if "://" in value else f"//{value}").hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified or address.is_link_local


def _reason(name: str, value: str) -> str:
    if name.startswith(TOKEN_PREFIXES):
        return "account"
    if name in TICK_ONLY:
        return "tick only"
    if value.startswith(("/", "~")):
        return "host path"
    return "host local address" if _local(value) else ""


def entries(environ: Mapping[str, str], home: Path) -> tuple[dict[str, str], dict[str, str]]:
    kept, left_out = {}, {}
    for name in names(home):
        value = environ.get(name)
        if value is None:
            continue
        reason = _reason(name, value)
        if reason:
            left_out[name] = reason
        else:
            kept[name] = value
    return kept | bundle_origin.origin(home, environ) | {
        linked_profiles.ENV_KEY: linked_profiles.sources(home)
    }, left_out


def publish(environ: Mapping[str, str], path: str, dry_run: bool, opener: Callable | None = None) -> str:
    kept, left_out = entries(environ, registry.home(environ))
    skipped = ", ".join(f"{name} ({reason})" for name, reason in sorted(left_out.items())) or "nothing"
    linked = "a bundle is linked" if kept[bundle_origin.URL_KEY] else "no bundle is linked"
    if dry_run:
        written = f"would write {', '.join(sorted(kept))}"
        return f"dry run, nothing written to OpenBao {openbao_kv.MOUNT}/{path}: {written}; left out {skipped}; {linked}"
    total = bundle_origin.write(environ, path, kept, opener)
    return f"{openbao_kv.summary(path, kept, total)}; left out {skipped}; {linked}"
