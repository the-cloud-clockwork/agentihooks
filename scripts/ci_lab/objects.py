"""The lab object layout, lab/<kind>/<digest>/{report.json,log.txt} and lab/grades/<run>/, on local or S3 storage."""

import os
import re
import uuid
from pathlib import Path
from typing import Any, Protocol

from scripts.swarm_v2.artifacts.local import LocalBackend
from scripts.swarm_v2.artifacts.object_store import ObjectStoreBackend
from scripts.swarm_v2.filesystem import contain

__all__ = [
    "ROOT",
    "LocalObjects",
    "Objects",
    "S3Objects",
    "Uploads",
    "grade_prefix",
    "is_report",
    "manifest_folder",
    "unit_path",
    "upload_path",
]

ROOT = "lab/"
FILES = {"report": "report.json", "log": "log.txt"}
UPLOAD_SECONDS = 900
KEY = re.compile(r"(?P<kind>[a-z]+)-v[0-9]+-(?P<digest>[0-9a-f]{64})")
REPORT = re.compile(ROOT + r"[a-z]+/[0-9a-f]{64}/report\.json")


class Objects(Protocol):
    def write_once(self, path: str, data: bytes) -> bool: ...

    def read(self, path: str) -> bytes: ...

    def keys(self, prefix: str) -> list[str]: ...

    def remove(self, path: str) -> None: ...

    def upload_url(self, path: str, expires_s: int) -> str: ...


def unit_path(kind: str, key: str, name: str) -> str:
    """Return the path of a unit's report or log, lab/<kind>/<digest>/<file>, refusing a key of another kind."""
    match = KEY.fullmatch(key)
    if match is None or match["kind"] != kind:
        raise ValueError(f"not a {kind} unit key: {key}")
    return f"{ROOT}{kind}/{match['digest']}/{FILES[name]}"


def grade_prefix(run: str) -> str:
    """Return the folder of one grade run."""
    return f"{ROOT}grades/{run}/"


def upload_path(job: str, name: str) -> str:
    """Return where a worker uploads a leased job's report or log."""
    return f"{ROOT}uploads/{job}/{FILES[name]}"


def manifest_folder(slug: str) -> str:
    """Return the folder of a repository's packed dependency manifests, one <commit>.json per dev commit."""
    return f"{ROOT}manifests/{slug.replace('/', '--')}/"


def is_report(path: str) -> bool:
    """Whether a path is the report of a stored unit verdict."""
    return REPORT.fullmatch(path) is not None


class LocalObjects:
    """Objects in a local directory: atomic writes, write once by hard link, file URLs for uploads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.backend = LocalBackend(root)

    def write_once(self, path: str, data: bytes) -> bool:
        """Write an object only when its path is free; return whether this call wrote it."""
        staging = path + uuid.uuid4().hex
        self.backend.write(staging, data)
        try:
            os.link(contain(self.root, staging), contain(self.root, path))
        except FileExistsError:
            return False
        finally:
            self.backend.remove(staging)
        return True

    def read(self, path: str) -> bytes:
        """Return an object's bytes."""
        return contain(self.root, path).read_bytes()

    def keys(self, prefix: str) -> list[str]:
        """Return the sorted paths under a prefix."""
        return self.backend.keys(prefix)

    def remove(self, path: str) -> None:
        """Remove an object; a missing one is already gone."""
        self.backend.remove(path)

    def upload_url(self, path: str, expires_s: int) -> str:
        """Return the file URL a local worker writes the object to; a local file never expires."""
        return contain(self.root, path).as_uri()


class S3Objects:
    """Objects in an S3 compatible bucket such as MinIO: conditional puts and presigned PUT URLs."""

    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix
        self.backend = ObjectStoreBackend(client, bucket, prefix)

    def write_once(self, path: str, data: bytes) -> bool:
        """Put an object only when its key is free; return whether this call wrote it."""
        return self.backend.write_once(path, data)

    def read(self, path: str) -> bytes:
        """Return an object's bytes."""
        return self.client.get_object(Bucket=self.bucket, Key=self.prefix + path)["Body"].read()

    def keys(self, prefix: str) -> list[str]:
        """Return the sorted paths under a prefix."""
        return self.backend.keys(prefix)

    def remove(self, path: str) -> None:
        """Remove an object; a missing one is already gone."""
        self.backend.remove(path)

    def upload_url(self, path: str, expires_s: int) -> str:
        """Return a presigned PUT URL for the object."""
        params = {"Bucket": self.bucket, "Key": self.prefix + path}
        return self.client.generate_presigned_url("put_object", Params=params, ExpiresIn=expires_s)


class Uploads:
    """The lease API upload signer: presigned PUT URLs for a leased job's report and log."""

    def __init__(self, objects: Objects, expires_s: int = UPLOAD_SECONDS) -> None:
        self.objects = objects
        self.expires_s = expires_s

    def upload_url(self, job: str, name: str) -> str:
        """Return the URL a worker puts the job's report or log to."""
        return self.objects.upload_url(upload_path(job, name), self.expires_s)
