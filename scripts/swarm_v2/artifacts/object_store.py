"""An S3 shaped object store backend; the client, bucket and endpoint come from the caller's configuration."""

from typing import Any

from scripts.swarm_v2.artifacts.base import Ack, ArtifactError

MISSING = frozenset({"404", "NoSuchKey", "NotFound"})
PAGES = 10_000


def _missing(error: Exception) -> bool:
    return getattr(error, "response", {}).get("Error", {}).get("Code") in MISSING


class ObjectStoreBackend:
    kind = "object-store"

    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def _request(self, key: str) -> dict:
        return {"Bucket": self.bucket, "Key": self.prefix + key}

    def write(self, key: str, data: bytes) -> Ack:
        response = self.client.put_object(**self._request(key), Body=data)
        return Ack(len(data), response.get("ETag", "").strip('"'))

    def size(self, key: str) -> int | None:
        try:
            response = self.client.head_object(**self._request(key))
        except Exception as error:
            if _missing(error):
                return None
            raise
        return response["ContentLength"]

    def read(self, key: str, start: int, length: int) -> bytes:
        if not length:
            return b""
        response = self.client.get_object(**self._request(key), Range=f"bytes={start}-{start + length - 1}")
        return response["Body"].read()

    def move(self, source: str, target: str) -> None:
        self.client.copy_object(**self._request(target), CopySource=self._request(source))
        self.remove(source)

    def remove(self, key: str) -> None:
        self.client.delete_object(**self._request(key))

    def keys(self, prefix: str) -> list[str]:
        request = {"Bucket": self.bucket, "Prefix": self.prefix + prefix}
        found = []
        for _ in range(PAGES):
            page = self.client.list_objects_v2(**request)
            found.extend(item["Key"].removeprefix(self.prefix) for item in page.get("Contents", ()))
            if not page.get("IsTruncated"):
                return sorted(found)
            request["ContinuationToken"] = page["NextContinuationToken"]
        raise ArtifactError(f"listing {prefix} did not finish within {PAGES} pages")
