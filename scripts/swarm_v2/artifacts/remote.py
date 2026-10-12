import os
from collections.abc import Mapping

from scripts.swarm_v2.artifacts.base import Backend
from scripts.swarm_v2.artifacts.object_store import ObjectStoreBackend

BUCKET = "AGENTIHOOKS_CHECKPOINT_BUCKET"
PREFIX = "AGENTIHOOKS_CHECKPOINT_PREFIX"


def controller_backend(environ: Mapping[str, str] = os.environ) -> Backend | None:
    bucket = environ.get(BUCKET)
    if not bucket:
        return None
    import boto3

    return ObjectStoreBackend(boto3.client("s3"), bucket, environ.get(PREFIX, "checkpoints/"))
