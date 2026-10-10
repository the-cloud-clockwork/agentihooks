"""Publish an attempt file as a checkpoint artifact; lost shared storage pauses publication and leaves the file alone."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.swarm_v2.artifacts.base import ArtifactRef, ArtifactStore, Scope

PUBLISHED = "published"
PAUSED = "paused"
SWITCH = "SWARM_ARTIFACT_PUBLISHING"


@dataclass(frozen=True)
class Publication:
    state: str
    ref: ArtifactRef | None
    reason: str


def publish(
    store: ArtifactStore, scope: Scope, artifact_id: str, source: Path, environ: Mapping[str, str] = os.environ
) -> Publication:
    data = source.read_bytes()
    if environ.get(SWITCH) == "off":
        return Publication(PAUSED, None, f"{SWITCH} is off, so publication is paused and the attempt keeps its files")
    try:
        return Publication(PUBLISHED, store.put(scope, artifact_id, data), "")
    except OSError as error:
        cause = error.strerror or type(error).__name__
        reason = f"artifact storage is unavailable ({cause}), so publication is paused and the attempt keeps its files"
        return Publication(PAUSED, None, reason)
