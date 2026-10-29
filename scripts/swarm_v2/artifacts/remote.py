import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from scripts.swarm_v2.artifacts.base import ArtifactError, ArtifactStore
from scripts.swarm_v2.artifacts.object_store import ConditionalBackend, ObjectStoreBackend
from scripts.swarm_v2.auth_context import Registration
from scripts.swarm_v2.broadcast_bridge import GRANT_FILE, grant_path

BUCKET = "AGENTIHOOKS_CHECKPOINT_BUCKET"
PREFIX = "AGENTIHOOKS_CHECKPOINT_PREFIX"


def controller_backend(environ: Mapping[str, str] = os.environ) -> ConditionalBackend | None:
    bucket = environ.get(BUCKET)
    if not bucket:
        return None
    import boto3

    prefix = environ.get(PREFIX, "checkpoints").rstrip("/") + "/"
    return ObjectStoreBackend(boto3.client("s3"), bucket, prefix)


class ControllerBackend:
    kind = "object-store"

    def __init__(self, url: str, credential: Path) -> None:
        self.url = url.rstrip("/") + "/v2/artifacts/backend"
        self.credential = credential

    def request(self, operation: str, token: str | None = None, **fields: object) -> dict:
        token = self.credential.read_bytes().decode().strip() if token is None else token
        request = urllib.request.Request(
            self.url,
            json.dumps({"operation": operation, **fields}).encode(),
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = json.loads(error.read())
            raise ArtifactError(f"controller artifact request refused: {detail['error_class']}") from None
        except urllib.error.URLError:
            raise ArtifactError("controller artifact storage is unavailable") from None

    def write(self, key: str, data: bytes) -> None:
        self.request("write", key=key, data=base64.b64encode(data).decode())

    def size(self, key: str) -> int | None:
        return self.request("size", key=key)["size"]

    def read(self, key: str, start: int, length: int) -> bytes:
        return base64.b64decode(self.request("read", key=key, start=start, length=length)["data"], validate=True)

    def move(self, source: str, target: str) -> None:
        self.request("move", key=source, target=target)

    def remove(self, key: str) -> None:
        self.request("remove", key=key)

    def keys(self, prefix: str) -> list[str]:
        return self.request("keys", key=prefix)["keys"]


def worker_store(spec: dict, attempt: Path, environ: Mapping[str, str] = os.environ) -> ArtifactStore:
    credential = Path(environ.get(GRANT_FILE, str(grant_path(attempt))))
    backend = ControllerBackend(spec["control_url"], credential)
    token = credential.read_bytes().decode().strip()
    return ArtifactStore(backend, lambda grant: Registration(**backend.request("scope", grant)["registration"]), token)
