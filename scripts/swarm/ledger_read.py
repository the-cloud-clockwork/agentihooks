"""A ledger's overview and phases: from the ledger folder on a local host, through the hosted ledger server when deployed."""

import os

from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_link


def parts(slug: str, *keys: str) -> dict:
    if not ledger_link.remote(os.environ):
        from scripts.swarm_ledger.repository.folder import ledger_folder
        from scripts.swarm_ledger.storage import read_ledger

        return read_ledger(ledger_folder(os.environ), slug, *keys) or {}
    return _hosted(slug, keys)


def _hosted(slug: str, keys: tuple) -> dict:
    from scripts.swarm_ledger import ledger

    credential = os.environ.get("AGENTIHOOKS_CONTROLLER_CREDENTIAL")
    headers = {"X-Controller-Credential": credential} if credential else None
    try:
        metadata = ledger.resource(slug, "metadata", headers=headers)
        found = {key: metadata[key] for key in keys if key in metadata}
        if "phases" in keys:
            found["phases"] = ledger.resource(slug, "phases", collection=True, headers=headers)
    except (OSError, SystemExit) as error:
        if getattr(error, "code", None) != 404:
            raise SwarmError(f"the hosted ledger is unavailable for {slug}: {error}") from None
        return {}
    return found
