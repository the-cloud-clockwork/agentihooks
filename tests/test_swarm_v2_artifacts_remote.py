import io
from unittest.mock import Mock

import pytest

from scripts.swarm_v2.artifacts import remote


class Objects:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Bucket, Key] = Body

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Bucket, Key])}

    def get_object(self, Bucket, Key, Range):
        start, end = map(int, Range.removeprefix("bytes=").split("-"))
        return {"Body": io.BytesIO(self.objects[Bucket, Key][start : end + 1])}

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Bucket, Key] = self.objects[CopySource["Bucket"], CopySource["Key"]]

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(self, Bucket, Prefix):
        return {
            "Contents": [{"Key": key} for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)]
        }


def test_controller_storage_is_disabled_without_a_bucket(monkeypatch):
    client = Mock()
    monkeypatch.setattr("boto3.client", client)
    assert remote.controller_backend({}) is None
    client.assert_not_called()


@pytest.mark.parametrize("prefix", ["checkpoints", "checkpoints/"])
def test_controller_uses_private_bucket_and_verifies_a_round_trip(monkeypatch, prefix):
    objects = Objects()
    client = Mock(return_value=objects)
    monkeypatch.setattr("boto3.client", client)
    backend = remote.controller_backend(
        {"AGENTIHOOKS_CHECKPOINT_BUCKET": "checkpoint-fixture", "AGENTIHOOKS_CHECKPOINT_PREFIX": prefix}
    )
    assert backend.kind == "object-store"
    client.assert_called_once_with("s3")
    backend.write("swarm/task/staging/execution/checkpoint", b"portable checkpoint")
    assert backend.size("swarm/task/staging/execution/checkpoint") == 19
    assert backend.read("swarm/task/staging/execution/checkpoint", 9, 10) == b"checkpoint"
    backend.move("swarm/task/staging/execution/checkpoint", "swarm/task/objects/checkpoint")
    assert backend.keys("swarm/task/") == ["swarm/task/objects/checkpoint"]
    assert objects.objects == {
        ("checkpoint-fixture", "checkpoints/swarm/task/objects/checkpoint"): b"portable checkpoint"
    }
