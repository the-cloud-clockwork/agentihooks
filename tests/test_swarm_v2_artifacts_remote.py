import io
from unittest.mock import Mock

import pytest

from scripts.swarm_v2.artifacts import remote


class Objects:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
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


def test_default_and_custom_controller_prefixes(monkeypatch):
    objects = Objects()
    monkeypatch.setattr("boto3.client", Mock(return_value=objects))
    for settings, expected in [
        ({}, "checkpoints/swarm/task/value"),
        ({"AGENTIHOOKS_CHECKPOINT_PREFIX": "private"}, "private/swarm/task/value"),
        ({"AGENTIHOOKS_CHECKPOINT_PREFIX": "private/"}, "private/swarm/task/value"),
        ({"AGENTIHOOKS_CHECKPOINT_PREFIX": "prefixX"}, "prefixX/swarm/task/value"),
    ]:
        backend = remote.controller_backend({"AGENTIHOOKS_CHECKPOINT_BUCKET": "bucket", **settings})
        backend.write("swarm/task/value", b"value")
        assert objects.objects["bucket", expected] == b"value"


def test_conditional_creation_reports_new_and_existing_objects():
    from scripts.swarm_v2.artifacts.object_store import ObjectStoreBackend

    backend = ObjectStoreBackend(Objects(), "bucket", "checkpoints/")
    assert backend.write_once("key", b"first") is True
    assert backend.write_once("key", b"other") is False
    assert backend.read("key", 0, 5) == b"first"


@pytest.mark.parametrize("code", ["PreconditionFailed", "412", "ConditionalRequestConflict", "409"])
def test_conditional_collision_is_not_overwritten(code):
    from botocore.exceptions import ClientError

    from scripts.swarm_v2.artifacts.object_store import ObjectStoreBackend

    client = Mock()
    client.put_object.side_effect = ClientError({"Error": {"Code": code}}, "PutObject")
    backend = ObjectStoreBackend(client, "bucket", "checkpoints/")
    assert backend.write_once("key", b"value") is False
    client.put_object.assert_called_once_with(Bucket="bucket", Key="checkpoints/key", Body=b"value", IfNoneMatch="*")


def test_other_object_store_failures_remain_visible():
    from botocore.exceptions import ClientError

    from scripts.swarm_v2.artifacts.object_store import ObjectStoreBackend

    client = Mock()
    failure = ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
    client.put_object.side_effect = failure
    with pytest.raises(ClientError) as caught:
        ObjectStoreBackend(client, "bucket", "checkpoints/").write_once("key", b"value")
    assert caught.value is failure


def test_worker_request_preserves_base_path_and_wire_contract(tmp_path, monkeypatch):
    import json

    credential = tmp_path / "grant"
    credential.write_text("grant")
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"size":null}'
    send = Mock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", send)
    backend = remote.ControllerBackend("http://controller/endpointX/", credential)
    assert backend.size("swarm/task/objects/value") is None
    call = send.call_args
    packet = call.args[0]
    assert packet.full_url == "http://controller/endpointX/v2/artifacts/backend"
    assert packet.get_method() == "POST"
    assert packet.get_header("Authorization") == "Bearer grant"
    assert packet.get_header("Content-type") == "application/json"
    assert json.loads(packet.data) == {"operation": "size", "key": "swarm/task/objects/value"}
    assert call.kwargs == {"timeout": 30}


def test_unavailable_controller_is_reported_without_credentials(tmp_path, monkeypatch):
    import urllib.error

    from scripts.swarm_v2.artifacts.base import ArtifactError

    credential = tmp_path / "grant"
    credential.write_text("grant")
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=urllib.error.URLError("unavailable")))
    with pytest.raises(ArtifactError) as caught:
        remote.ControllerBackend("http://controller", credential).keys("swarm/task/")
    assert str(caught.value) == "controller artifact storage is unavailable"


def test_worker_read_rejects_invalid_base64(tmp_path, monkeypatch):
    import binascii

    credential = tmp_path / "grant"
    credential.write_text("grant")
    backend = remote.ControllerBackend("http://controller", credential)
    monkeypatch.setattr(backend, "request", lambda *args, **kwargs: {"data": "%%"})
    with pytest.raises(binascii.Error):
        backend.read("swarm/task/objects/value", 0, 1)
