from scripts.swarm_ledger import ledger
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
