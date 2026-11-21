import json
from typing import Any


def configured_manifest(suite: Any) -> dict[str, Any]:
    # ruleid: lab-worker-local-manifest
    return json.loads(suite.manifest.read_text())


def leased_manifest(lease: Any) -> dict[str, Any]:
    # ok: lab-worker-local-manifest
    return json.loads(lease.unit.manifest)
