"""The lab object layout, lab/<kind>/<digest>/{report.json,log.txt} and lab/grades/<run>/, on local or S3 storage."""

from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "ROOT",
    "LocalObjects",
    "Objects",
    "S3Objects",
    "Uploads",
    "grade_prefix",
    "is_report",
    "unit_path",
    "upload_path",
]

ROOT = "lab/"
UPLOAD_SECONDS = 900


class Objects(Protocol):
    def write_once(self, path: str, data: bytes) -> bool: ...

    def read(self, path: str) -> bytes: ...

    def keys(self, prefix: str) -> list[str]: ...

    def upload_url(self, path: str, expires_s: int) -> str: ...


def unit_path(kind: str, key: str, name: str) -> str:
    """Return the path of a unit's report or log, lab/<kind>/<digest>/<file>, refusing a key of another kind."""
    raise NotImplementedError


def grade_prefix(run: str) -> str:
    """Return the folder of one grade run."""
    raise NotImplementedError


def upload_path(job: str, name: str) -> str:
    """Return where a worker uploads a leased job's report or log."""
    raise NotImplementedError


def is_report(path: str) -> bool:
    """Whether a path is the report of a stored unit verdict."""
    raise NotImplementedError


class LocalObjects:
    """Objects in a local directory: atomic writes, write once by hard link, file URLs for uploads."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write_once(self, path: str, data: bytes) -> bool:
        """Write an object only when its path is free; return whether this call wrote it."""
        raise NotImplementedError

    def read(self, path: str) -> bytes:
        """Return an object's bytes."""
        raise NotImplementedError

    def keys(self, prefix: str) -> list[str]:
        """Return the sorted paths under a prefix."""
        raise NotImplementedError

    def upload_url(self, path: str, expires_s: int) -> str:
        """Return the file URL a local worker writes the object to."""
        raise NotImplementedError


class S3Objects:
    """Objects in an S3 compatible bucket such as MinIO: conditional puts and presigned PUT URLs."""

    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self.client = client

    def write_once(self, path: str, data: bytes) -> bool:
        """Put an object only when its key is free; return whether this call wrote it."""
        raise NotImplementedError

    def read(self, path: str) -> bytes:
        """Return an object's bytes."""
        raise NotImplementedError

    def keys(self, prefix: str) -> list[str]:
        """Return the sorted paths under a prefix."""
        raise NotImplementedError

    def upload_url(self, path: str, expires_s: int) -> str:
        """Return a presigned PUT URL for the object."""
        raise NotImplementedError


class Uploads:
    """The lease API upload signer: presigned PUT URLs for a leased job's report and log."""

    def __init__(self, objects: Objects, expires_s: int = UPLOAD_SECONDS) -> None:
        self.objects = objects

    def upload_url(self, job: str, name: str) -> str:
        """Return the URL a worker puts the job's report or log to."""
        raise NotImplementedError
