import urllib.error

from scripts.swarm import ledger_client
from scripts.swarm_ledger import ledger
from scripts.tick import view as ledger_view
from scripts.swarm_ledger.api.client import ResourceClient

REQUEST_TIMEOUT = 10
EXPORT_TIMEOUT = 30


def short_export(base: str, slug: str) -> dict:
    # ruleid: export-read-with-write-timeout
    return ResourceClient(base, {}, REQUEST_TIMEOUT).request(slug, "export", {})


def short_module_export(base: str, slug: str) -> dict:
    # ruleid: export-read-with-write-timeout
    return ResourceClient(base, {}, ledger.REQUEST_TIMEOUT).request(slug, "export", {})


def sized_export(base: str, slug: str) -> dict:
    # ok: export-read-with-write-timeout
    return ResourceClient(base, {}, EXPORT_TIMEOUT).request(slug, "export", {})


def short_resource(base: str, slug: str) -> dict:
    # ok: export-read-with-write-timeout
    return ResourceClient(base, {}, REQUEST_TIMEOUT).request(slug, "metadata")


def _hold_stored(slug: str, exc: OSError) -> None:
    del slug, exc


def _read_long_or_hold_stored(slug: str, service: bool, exc: OSError) -> None:
    del slug, service, exc


def short_view_only(slug: str, service: bool) -> None:
    # ruleid: view-start-without-export-retry
    try:
        ledger_view.start(slug, service, ledger_client.SNAPSHOT_TIMEOUT_S)
    except OSError as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
            raise
        _hold_stored(slug, exc)


def short_view_with_retry(slug: str, service: bool) -> None:
    # ok: view-start-without-export-retry
    try:
        ledger_view.start(slug, service, ledger_client.SNAPSHOT_TIMEOUT_S)
    except OSError as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
            raise
        _read_long_or_hold_stored(slug, service, exc)
