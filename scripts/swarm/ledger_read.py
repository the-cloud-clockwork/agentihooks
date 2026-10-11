"""A ledger's overview and phases: from the ledger folder on a local host, through the hosted ledger server when deployed."""

import os
import urllib.error

from scripts.swarm_ledger import ledger_link


def parts(slug: str, *keys: str) -> dict:
    if not ledger_link.remote(os.environ):
        from scripts.swarm_ledger.repository.folder import ledger_folder
        from scripts.swarm_ledger.repository.sqlite import read_ledger

        return read_ledger(ledger_folder(os.environ), slug, *keys) or {}
    return _hosted(slug, keys)


def _hosted(slug: str, keys: tuple) -> dict:
    from scripts.swarm_ledger import ledger

    try:
        metadata = ledger.resource(slug, "metadata")
        found = {key: metadata[key] for key in keys if key in metadata}
        if "phases" in keys:
            found["phases"] = ledger.resource(slug, "phases", collection=True)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return {}
    return found
