import hashlib
import json
import os
from collections import Counter

import pytest

from scripts.swarm_v2.artifacts import base, local, object_store
from scripts.swarm_v2.auth_context import Registration

pytestmark = pytest.mark.unit

KINDS = ("local", "object-store")
DATA = b"artifact body: 0123456789abcdef"
OTHER = b"another artifact body"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def registration(task: str = "t1", execution: str = "e1", generation: int = 2) -> Registration:
    return Registration(
        grant_id="lgr-" + "0" * 32,
        swarm_id="s1",
        execution_id=execution,
        generation=generation,
        seat_id="eng-1",
        task_id=task,
        account="acct",
        brain_id="brain",
        project_ids=["local:fixture"],
        issuer="issuer",
        audience="audience",
        key_id="key",
        registered_at="2026-10-10T00:00:00Z",
    )


class Missing(Exception):
    def __init__(self, code: str = "NoSuchKey"):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Body:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakeObjectStore:
    def __init__(self, page_size: int = 1000):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.page_size = page_size
        self.truncate_puts = 0
        self.truncate_copies = 0
        self.calls: Counter[str] = Counter()
        self.requests: list[tuple[str, dict]] = []

    def _record(self, name: str, request: dict) -> None:
        self.calls[name] += 1
        self.requests.append((name, request))

    def _object(self, request: dict) -> bytes:
        try:
            return self.objects[(request["Bucket"], request["Key"])]
        except KeyError:
            raise Missing() from None

    def put_object(self, **request):
        self._record("put_object", request)
        body = request["Body"]
        stored = body
        if self.truncate_puts:
            self.truncate_puts -= 1
            stored = body[: len(body) // 2]
        self.objects[(request["Bucket"], request["Key"])] = stored
        return {"ETag": f'"{hashlib.md5(body).hexdigest()}"'}

    def head_object(self, **request):
        self._record("head_object", request)
        return {"ContentLength": len(self._object(request))}

    def get_object(self, **request):
        self._record("get_object", request)
        data = self._object(request)
        first, last = request["Range"].removeprefix("bytes=").split("-")
        return {"Body": Body(data[int(first) : int(last) + 1])}

    def copy_object(self, **request):
        self._record("copy_object", request)
        source = request["CopySource"]
        data = self._object({"Bucket": source["Bucket"], "Key": source["Key"]})
        if self.truncate_copies:
            self.truncate_copies -= 1
            data = data[: len(data) // 2]
        self.objects[(request["Bucket"], request["Key"])] = data
        return {}

    def delete_object(self, **request):
        self._record("delete_object", request)
        self.objects.pop((request["Bucket"], request["Key"]), None)
        return {}

    def list_objects_v2(self, **request):
        self._record("list_objects_v2", request)
        keys = sorted(k for b, k in self.objects if b == request["Bucket"] and k.startswith(request["Prefix"]))
        start = int(request.get("ContinuationToken", "0"))
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        response = {"IsTruncated": more, "KeyCount": len(page)}
        if page:
            response["Contents"] = [{"Key": key} for key in page]
        if more:
            response["NextContinuationToken"] = str(start + self.page_size)
        return response


class World:
    def __init__(self, kind: str, tmp_path):
        self.kind = kind
        self.fake = FakeObjectStore()
        if kind == "local":
            self.backend = local.LocalBackend(tmp_path / "durable")
        else:
            self.backend = object_store.ObjectStoreBackend(self.fake, "bucket", "swarm/")
        self.writes: list[str] = []
        self.cut = 0
        self.flip = 0
        real = self.backend.write

        def write(key: str, data: bytes) -> None:
            self.writes.append(key)
            if self.cut:
                self.cut -= 1
                data = data[: len(data) // 2]
            elif self.flip:
                self.flip -= 1
                data = bytes([data[0] ^ 1]) + data[1:]
            real(key, data)

        self.backend.write = write
        self.store = base.ArtifactStore(self.backend)
        self.scope = base.Scope.granted(registration())

    def truncate(self, uploads: int = 1) -> None:
        if self.kind == "local":
            self.cut = uploads
        else:
            self.fake.truncate_puts = uploads

    def keys(self) -> list[str]:
        return self.backend.keys("s1/")

    def document(self, key: str) -> dict:
        return json.loads(self.backend.read(key, 0, self.backend.size(key)))


@pytest.fixture(params=KINDS)
def world(request, tmp_path):
    return World(request.param, tmp_path)


def object_key(data: bytes) -> str:
    return f"s1/t1/objects/{sha(data)}"


def staging_key(data: bytes) -> str:
    return f"s1/t1/staging/e1/{sha(data)}"


def record_bytes(data: bytes, execution: str = "e1", generation: int = 2) -> bytes:
    document = {"execution_id": execution, "generation": generation, "sha256": sha(data), "size": len(data)}
    return json.dumps(document, sort_keys=True).encode()


def test_published_names_are_stable():
    assert base.METRIC == "artifact_upload_verification_failures"
    assert (base.ABSENT, base.CORRUPT, base.VERIFIED) == ("absent", "corrupt", "verified")
    assert base.CHUNK == 1 << 20
    assert local.TEMPORARY == ".partial-"
    assert object_store.PAGES == 10_000
    assert (local.LocalBackend.kind, object_store.ObjectStoreBackend.kind) == KINDS


def test_every_refused_upload_is_counted(world):
    world.truncate(2)
    for _ in range(2):
        with pytest.raises(base.ArtifactError):
            world.store.put(world.scope, "a1", DATA)
    assert world.store.metrics() == {base.METRIC: {world.kind: 2}}


def test_reference_is_the_content_digest_and_size():
    ref = base.ArtifactRef.of(DATA)
    assert ref == base.ArtifactRef(sha(DATA), len(DATA))
    assert base.ArtifactRef.of(b"") == base.ArtifactRef(sha(b""), 0)


@pytest.mark.parametrize(
    ("digest", "size"),
    [("a" * 63, 1), ("a" * 65, 1), ("A" * 64, 1), ("g" * 64, 1), ("a" * 64, -1), (" " + "a" * 63, 1)],
)
def test_malformed_reference_is_refused(digest, size):
    with pytest.raises(base.ArtifactError) as raised:
        base.ArtifactRef(digest, size)
    assert str(raised.value) == f"not a content reference: {digest}:{size}"


def test_scope_comes_from_the_verified_grant():
    scope = base.Scope.granted(registration(task="t9", execution="e7", generation=4))
    assert scope == base.Scope("s1", "t9", "e7", 4)
    assert scope.key("objects", "x") == "s1/t9/objects/x"


@pytest.mark.parametrize("field", ["swarm_id", "task_id", "execution_id"])
@pytest.mark.parametrize("value", ["../x", "", ".hidden", "a/b", 7])
def test_scope_refuses_names_that_are_not_identifiers(field, value):
    names = {"swarm_id": "s1", "task_id": "t1", "execution_id": "e1", field: value}
    with pytest.raises(base.ArtifactError) as raised:
        base.Scope(**names, generation=1)
    assert str(raised.value) == f"not an identifier: {value!r}"


def test_put_verifies_and_retrieves_by_content_reference(world):
    ref = world.store.put(world.scope, "a1", DATA)
    assert ref == base.ArtifactRef(sha(DATA), len(DATA))
    assert world.store.stat(world.scope, ref) == base.VERIFIED
    assert world.store.get_range(world.scope, ref) == DATA
    assert world.store.get_range(world.scope, ref, 2, 3) == DATA[2:5]
    assert world.store.get_range(world.scope, ref, 3) == DATA[3:]
    assert world.store.recorded(world.scope, "a1") == ref
    assert world.keys() == ["s1/t1/artifacts/a1.json", object_key(DATA)]
    assert world.writes == [staging_key(DATA), staging_key(record_bytes(DATA))]
    assert world.document("s1/t1/artifacts/a1.json") == json.loads(record_bytes(DATA))
    assert world.store.metrics() == {base.METRIC: {}}


def test_empty_artifact_round_trips(world):
    ref = world.store.put(world.scope, "empty", b"")
    assert world.store.stat(world.scope, ref) == base.VERIFIED
    assert world.store.get_range(world.scope, ref) == b""


def test_identical_content_is_stored_once(world):
    first = world.store.put(world.scope, "a1", DATA)
    second = world.store.put(world.scope, "a2", DATA)
    assert first == second
    assert world.keys() == ["s1/t1/artifacts/a1.json", "s1/t1/artifacts/a2.json", object_key(DATA)]
    assert world.writes.count(staging_key(DATA)) == 1


def test_truncated_upload_is_refused_counted_and_leaves_no_state(world):
    world.store.put(world.scope, "a1", OTHER)
    before = {key: world.backend.read(key, 0, world.backend.size(key)) for key in world.keys()}
    world.truncate()
    with pytest.raises(base.ArtifactError) as raised:
        world.store.put(world.scope, "a2", DATA)
    assert str(raised.value) == (
        f"{world.kind} reported a successful write of {len(DATA)} bytes to {staging_key(DATA)} "
        f"but the read back does not match {sha(DATA)}"
    )
    assert {key: world.backend.read(key, 0, world.backend.size(key)) for key in world.keys()} == before
    assert world.store.recorded(world.scope, "a2") is None
    assert world.store.stat(world.scope, base.ArtifactRef.of(DATA)) == base.ABSENT
    assert world.store.metrics() == {base.METRIC: {world.kind: 1}}


def test_retry_after_a_refused_upload_is_a_new_valid_request(world):
    world.truncate()
    with pytest.raises(base.ArtifactError):
        world.store.put(world.scope, "a1", DATA)
    ref = world.store.put(world.scope, "a1", DATA)
    assert world.store.get_range(world.scope, ref) == DATA
    assert world.store.metrics() == {base.METRIC: {world.kind: 1}}


def test_same_length_corruption_is_refused(world):
    world.flip = 1
    with pytest.raises(base.ArtifactError) as raised:
        world.store.put(world.scope, "a1", DATA)
    assert str(raised.value) == (
        f"{world.kind} reported a successful write of {len(DATA)} bytes to {staging_key(DATA)} "
        f"but the read back does not match {sha(DATA)}"
    )
    assert world.keys() == []
    assert world.store.metrics() == {base.METRIC: {world.kind: 1}}


def test_interrupted_upload_leaves_no_staging(world):
    real = world.backend.write

    def write(key, data):
        real(key, data)
        raise OSError("transport reset")

    world.backend.write = write
    with pytest.raises(OSError):
        world.store.put(world.scope, "a1", DATA)
    assert world.keys() == []


def test_every_range_read_verifies_the_whole_object(world):
    ref = world.store.put(world.scope, "a1", DATA)
    reads = []
    real = world.backend.read

    def read(key, start, length):
        reads.append((start, length))
        return real(key, start, length)

    world.backend.read = read
    world.store.get_range(world.scope, ref, 2, 3)
    world.store.get_range(world.scope, ref, 2, 3)
    assert reads == [(0, len(DATA)), (2, 3), (0, len(DATA)), (2, 3)]
    world.backend.write(object_key(DATA), DATA[:-1] + b"X")
    with pytest.raises(base.ArtifactError):
        world.store.get_range(world.scope, ref, 2, 3)


def test_deleted_artifact_is_no_longer_readable(world):
    ref = world.store.put(world.scope, "a1", DATA)
    world.store.get_range(world.scope, ref)
    assert world.store.delete_if_unreferenced(world.scope, ref) is True
    with pytest.raises(base.ArtifactError) as raised:
        world.store.get_range(world.scope, ref)
    assert str(raised.value) == f"artifact {sha(DATA)} is not verified in {world.kind}"


def test_stale_generation_cannot_delete_a_newer_record(world):
    newer = base.Scope.granted(registration(execution="e3", generation=3))
    ref = world.store.put(newer, "a1", DATA)
    keys = world.keys()
    assert world.store.delete_if_unreferenced(world.scope, ref) is False
    assert world.keys() == keys
    assert world.store.delete_if_unreferenced(newer, ref) is True


def test_truncated_record_keeps_the_verified_object_and_writes_no_record(world):
    world.store.put(world.scope, "warm", OTHER)
    world.writes.clear()
    real = world.backend.write

    def write(key, data):
        if key == staging_key(record_bytes(DATA)):
            world.truncate()
        return real(key, data)

    world.backend.write = write
    with pytest.raises(base.ArtifactError):
        world.store.put(world.scope, "a1", DATA)
    assert world.store.recorded(world.scope, "a1") is None
    assert world.store.stat(world.scope, base.ArtifactRef.of(DATA)) == base.VERIFIED
    world.backend.write = real
    world.writes.clear()
    world.store.put(world.scope, "a1", DATA)
    assert world.writes == [staging_key(record_bytes(DATA))]


def test_retrying_a_completed_upload_returns_the_committed_object_without_writing(world):
    ref = world.store.put(world.scope, "a1", DATA)
    keys = world.keys()
    world.writes.clear()
    assert world.store.put(world.scope, "a1", DATA) == ref
    assert world.writes == []
    assert world.keys() == keys


def test_same_artifact_id_with_other_content_is_refused_without_writing(world):
    world.store.put(world.scope, "a1", DATA)
    keys = world.keys()
    world.writes.clear()
    with pytest.raises(base.ArtifactError) as raised:
        world.store.put(world.scope, "a1", OTHER)
    assert str(raised.value) == "artifact a1 is committed with other content"
    assert world.writes == []
    assert world.keys() == keys


def test_lost_object_behind_a_record_is_republished_from_matching_content(world):
    ref = world.store.put(world.scope, "a1", DATA)
    world.backend.remove(object_key(DATA))
    assert world.store.stat(world.scope, ref) == base.ABSENT
    world.writes.clear()
    assert world.store.put(world.scope, "a1", DATA) == ref
    assert world.writes == [staging_key(DATA)]
    assert world.store.stat(world.scope, ref) == base.VERIFIED


@pytest.mark.parametrize("damage", [DATA[:-1], DATA[:-1] + b"X"])
def test_damaged_object_is_corrupt_unreadable_and_repaired_by_put(world, damage):
    ref = world.store.put(world.scope, "a1", DATA)
    world.backend.write(object_key(DATA), damage)
    assert world.store.stat(world.scope, ref) == base.CORRUPT
    with pytest.raises(base.ArtifactError) as raised:
        world.store.get_range(world.scope, ref, 0, 1)
    assert str(raised.value) == f"artifact {sha(DATA)} is not verified in {world.kind}"
    world.store.put(world.scope, "a1", DATA)
    assert world.store.get_range(world.scope, ref) == DATA


def test_unknown_reference_is_absent(world):
    assert world.store.stat(world.scope, base.ArtifactRef.of(DATA)) == base.ABSENT
    assert world.store.recorded(world.scope, "a1") is None


def test_another_task_scope_sees_nothing(world):
    ref = world.store.put(world.scope, "a1", DATA)
    other = base.Scope.granted(registration(task="t2"))
    assert world.store.stat(other, ref) == base.ABSENT
    assert world.store.recorded(other, "a1") is None


@pytest.mark.parametrize(("start", "length"), [(-1, 1), (0, -1), (0, len(DATA) + 1), (len(DATA), 1), (5, len(DATA))])
def test_range_outside_the_artifact_is_refused(world, start, length):
    ref = world.store.put(world.scope, "a1", DATA)
    with pytest.raises(base.ArtifactError) as raised:
        world.store.get_range(world.scope, ref, start, length)
    assert str(raised.value) == f"range {start}+{length} is outside {len(DATA)} bytes"


def test_range_edges_are_readable(world):
    ref = world.store.put(world.scope, "a1", DATA)
    assert world.store.get_range(world.scope, ref, len(DATA) - 1, 1) == DATA[-1:]
    assert world.store.get_range(world.scope, ref, len(DATA), 0) == b""
    assert world.store.get_range(world.scope, ref, len(DATA)) == b""


@pytest.mark.parametrize("artifact_id", ["../a", "", "a/b", ".a", 3])
def test_artifact_ids_must_be_identifiers(world, artifact_id):
    with pytest.raises(base.ArtifactError) as raised:
        world.store.put(world.scope, artifact_id, DATA)
    assert str(raised.value) == f"not an identifier: {artifact_id!r}"
    assert world.writes == []


def test_reads_large_artifacts_in_chunks(world, monkeypatch):
    monkeypatch.setattr(base, "CHUNK", 4)
    reads = []
    real = world.backend.read

    def read(key, start, length):
        reads.append((start, length))
        return real(key, start, length)

    world.backend.read = read
    data = b"0123456789"
    ref = world.store.put(world.scope, "a1", data)
    reads.clear()
    assert world.store.stat(world.scope, ref) == base.VERIFIED
    assert reads == [(0, 4), (4, 4), (8, 2)]


def counted_reads(world):
    reads = []
    real = world.backend.read

    def read(key, start, length):
        reads.append((start, length))
        return real(key, start, length)

    world.backend.read = read
    return reads


@pytest.mark.parametrize(
    ("data", "pieces", "expected"),
    [
        (b"0123456789", [b"0123", b"4567", b"89"], [(0, 4), (4, 4), (8, 2)]),
        (b"012345678", [b"0123", b"4567", b"8"], [(0, 4), (4, 4), (8, 1)]),
        (b"01234567", [b"0123", b"4567"], [(0, 4), (4, 4)]),
    ],
)
def test_stream_reads_each_piece_once_with_one_checksum_pass(world, monkeypatch, data, pieces, expected):
    monkeypatch.setattr(base, "CHUNK", 4)
    reads = counted_reads(world)
    ref = world.store.put(world.scope, "a1", data)
    reads.clear()
    assert list(world.store.stream(world.scope, ref)) == pieces
    assert reads == expected


@pytest.mark.parametrize("position", [0, 9])
def test_corrupted_object_fails_the_stream_before_its_last_piece(world, monkeypatch, position):
    monkeypatch.setattr(base, "CHUNK", 4)
    data = b"0123456789"
    ref = world.store.put(world.scope, "a1", data)
    damaged = bytearray(data)
    damaged[position] ^= 1
    world.backend.write(object_key(data), bytes(damaged))
    received = []
    with pytest.raises(base.ArtifactError) as raised:
        for piece in world.store.stream(world.scope, ref):
            received.append(piece)
    assert str(raised.value) == f"artifact {sha(data)} does not match its reference in {world.kind}"
    assert received == [bytes(damaged[:4]), bytes(damaged[4:8])]


@pytest.mark.parametrize("damage", [None, DATA[:-1], DATA + b"X"])
def test_stream_of_a_missing_or_resized_object_is_refused_before_reading(world, damage):
    ref = world.store.put(world.scope, "a1", DATA)
    world.backend.remove(object_key(DATA))
    if damage is not None:
        world.backend.write(object_key(DATA), damage)
    reads = counted_reads(world)
    with pytest.raises(base.ArtifactError) as raised:
        next(world.store.stream(world.scope, ref))
    assert str(raised.value) == f"artifact {sha(DATA)} is not verified in {world.kind}"
    assert reads == []


def test_empty_artifact_streams_no_pieces(world):
    ref = world.store.put(world.scope, "a1", b"")
    assert list(world.store.stream(world.scope, ref)) == []


def committed(world, *pairs):
    for artifact_id, data in pairs:
        world.store.put(world.scope, artifact_id, data)


def manifest(generation=2, execution="e1", artifacts=None):
    artifacts = {"a1": DATA, "a2": OTHER} if artifacts is None else artifacts
    return {
        "artifacts": {name: {"sha256": sha(data), "size": len(data)} for name, data in artifacts.items()},
        "execution_id": execution,
        "generation": generation,
        "name": "m",
    }


def test_manifest_commits_verified_artifacts_once(world):
    committed(world, ("a1", DATA), ("a2", OTHER))
    result = world.store.commit_manifest(world.scope, "m", ["a2", "a1", "a2"])
    assert result == manifest()
    assert list(result["artifacts"]) == ["a1", "a2"]
    assert world.document("s1/t1/manifests/m/2.json") == manifest()
    world.writes.clear()
    assert world.store.commit_manifest(world.scope, "m", ["a1", "a2"]) == manifest()
    assert world.writes == []


def test_manifest_with_other_content_at_the_same_generation_is_refused(world):
    committed(world, ("a1", DATA), ("a2", OTHER))
    world.store.commit_manifest(world.scope, "m", ["a1", "a2"])
    with pytest.raises(base.ArtifactError) as raised:
        world.store.commit_manifest(world.scope, "m", ["a1"])
    assert str(raised.value) == "manifest m generation 2 is committed with other content"
    sibling = base.Scope.granted(registration(execution="e2"))
    with pytest.raises(base.ArtifactError):
        world.store.commit_manifest(sibling, "m", ["a1", "a2"])
    assert world.document("s1/t1/manifests/m/2.json") == manifest()


def test_stale_generation_cannot_publish_over_a_newer_manifest(world):
    committed(world, ("a1", DATA), ("a2", OTHER))
    newer = base.Scope.granted(registration(execution="e3", generation=3))
    assert world.store.commit_manifest(newer, "m", ["a1"]) == manifest(3, "e3", {"a1": DATA})
    keys = world.keys()
    with pytest.raises(base.ArtifactError) as raised:
        world.store.commit_manifest(world.scope, "m", ["a1", "a2"])
    assert str(raised.value) == "manifest m has newer generation 3"
    assert world.keys() == keys
    newest = base.Scope.granted(registration(execution="e4", generation=4))
    assert world.store.commit_manifest(newest, "m", ["a2"]) == manifest(4, "e4", {"a2": OTHER})


def test_generation_fence_is_per_manifest_name(world):
    committed(world, ("a1", DATA))
    world.store.commit_manifest(base.Scope.granted(registration(generation=5)), "m2", ["a1"])
    assert world.store.commit_manifest(world.scope, "m", ["a1"]) == manifest(artifacts={"a1": DATA})


def test_manifest_refuses_an_absent_or_unverified_artifact(world):
    committed(world, ("a1", DATA))
    with pytest.raises(base.ArtifactError) as raised:
        world.store.commit_manifest(world.scope, "m", ["a1", "a9"])
    assert str(raised.value) == "artifact a9 is not committed and verified"
    world.backend.write(object_key(DATA), b"tampered")
    with pytest.raises(base.ArtifactError) as raised:
        world.store.commit_manifest(world.scope, "m", ["a1"])
    assert str(raised.value) == "artifact a1 is not committed and verified"
    assert not [key for key in world.keys() if "/manifests/" in key]


def test_manifest_name_must_be_an_identifier(world):
    with pytest.raises(base.ArtifactError) as raised:
        world.store.commit_manifest(world.scope, "../m", [])
    assert str(raised.value) == "not an identifier: '../m'"


def test_truncated_manifest_upload_publishes_no_manifest(world):
    committed(world, ("a1", DATA))
    world.truncate()
    with pytest.raises(base.ArtifactError):
        world.store.commit_manifest(world.scope, "m", ["a1"])
    assert not [key for key in world.keys() if "/manifests/" in key]
    assert world.store.metrics() == {base.METRIC: {world.kind: 1}}


def test_referenced_artifact_is_not_deleted(world):
    committed(world, ("a1", DATA), ("a2", OTHER))
    world.store.commit_manifest(world.scope, "m", ["a1"])
    keys = world.keys()
    assert world.store.delete_if_unreferenced(world.scope, base.ArtifactRef.of(DATA)) is False
    assert world.keys() == keys


def test_unreferenced_artifact_and_its_records_are_deleted(world):
    committed(world, ("a1", DATA), ("copy", DATA), ("a2", OTHER))
    world.store.commit_manifest(world.scope, "m", ["a2"])
    assert world.store.delete_if_unreferenced(world.scope, base.ArtifactRef.of(DATA)) is True
    assert world.keys() == ["s1/t1/artifacts/a2.json", "s1/t1/manifests/m/2.json", object_key(OTHER)]
    assert world.store.stat(world.scope, base.ArtifactRef.of(OTHER)) == base.VERIFIED


def test_local_backend_contains_every_key(tmp_path):
    backend = local.LocalBackend(tmp_path / "durable")
    with pytest.raises(base.ArtifactError) as raised:
        backend.write("../outside", b"x")
    assert str(raised.value) == "path resolves outside its execution root: ../outside"
    with pytest.raises(base.ArtifactError):
        backend.size("/etc/passwd")
    assert not (tmp_path / "outside").exists()


def test_local_write_syncs_the_file_and_its_directory(tmp_path, monkeypatch):
    backend = local.LocalBackend(tmp_path / "durable")
    synced = []
    real = os.fsync

    def fsync(fd):
        synced.append((os.readlink(f"/proc/self/fd/{fd}"), os.fstat(fd).st_size))
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    descriptors = len(os.listdir("/proc/self/fd"))
    backend.write("a/b/c", DATA)
    target = tmp_path.resolve() / "durable" / "a" / "b"
    assert [path for path, _ in synced][1:] == [str(target)]
    assert synced[0][0].startswith(str(target / ".partial-"))
    assert synced[0][1] == len(DATA)
    synced.clear()
    backend.move("a/b/c", "d/e/f")
    backend.write("a/b/g", OTHER)
    synced.clear()
    backend.move("a/b/g", "d/e/h")
    assert [path for path, _ in synced] == [str(tmp_path.resolve() / "durable" / "d" / "e")]
    assert backend.read("d/e/f", 0, len(DATA)) == DATA
    assert backend.size("a/b/c") is None
    assert len(os.listdir("/proc/self/fd")) == descriptors


def test_local_failed_write_leaves_no_partial_file(tmp_path, monkeypatch):
    backend = local.LocalBackend(tmp_path / "durable")

    def fail(fd):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError):
        backend.write("a/c", DATA)
    assert list((tmp_path / "durable" / "a").iterdir()) == []


def test_local_keys_skip_partials_and_missing_prefixes(tmp_path):
    backend = local.LocalBackend(tmp_path / "durable")
    assert backend.keys("nothing/") == []
    backend.write("p/x", b"1")
    backend.write("p/q/y", b"2")
    backend.write("pz", b"3")
    (tmp_path / "durable" / "p" / f"{local.TEMPORARY}junk").write_bytes(b"partial")
    assert backend.keys("p/") == ["p/q/y", "p/x"]
    backend.remove("p/x")
    backend.remove("p/x")
    assert backend.keys("p/") == ["p/q/y"]


def object_backend(fake=None):
    fake = fake or FakeObjectStore()
    return fake, object_store.ObjectStoreBackend(fake, "bucket", "swarm/")


def test_object_store_requests_are_scoped_to_bucket_and_prefix():
    fake, backend = object_backend()
    backend.write("k", DATA)
    assert fake.requests[-1] == ("put_object", {"Bucket": "bucket", "Key": "swarm/k", "Body": DATA})
    assert backend.read("k", 2, 3) == DATA[2:5]
    assert fake.requests[-1] == ("get_object", {"Bucket": "bucket", "Key": "swarm/k", "Range": "bytes=2-4"})
    assert backend.size("k") == len(DATA)
    backend.move("k", "m")
    assert fake.requests[-2:] == [
        ("copy_object", {"Bucket": "bucket", "Key": "swarm/m", "CopySource": {"Bucket": "bucket", "Key": "swarm/k"}}),
        ("delete_object", {"Bucket": "bucket", "Key": "swarm/k"}),
    ]
    assert set(fake.objects) == {("bucket", "swarm/m")}


def test_object_store_empty_read_sends_no_request():
    fake, backend = object_backend()
    backend.write("k", DATA)
    assert backend.read("k", 4, 0) == b""
    assert fake.calls["get_object"] == 0


def test_local_directory_key_has_no_size(tmp_path):
    backend = local.LocalBackend(tmp_path / "durable")
    backend.write("a/b", DATA)
    assert backend.size("a") is None


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_object_store_missing_object_has_no_size(code):
    class Gone(FakeObjectStore):
        def head_object(self, **request):
            raise Missing(code)

    _, backend = object_backend(Gone())
    assert backend.size("k") is None


@pytest.mark.parametrize("error", [Missing("AccessDenied"), RuntimeError("network")])
def test_object_store_other_errors_propagate(error):
    class Broken(FakeObjectStore):
        def head_object(self, **request):
            raise error

    _, backend = object_backend(Broken())
    with pytest.raises(type(error)):
        backend.size("k")


def test_object_store_keys_follow_every_page_and_drop_the_prefix():
    fake, backend = object_backend(FakeObjectStore(page_size=2))
    for key in ("p/a", "p/b", "p/c", "q/d"):
        backend.write(key, b"x")
    fake.objects[("other", "swarm/p/z")] = b"x"
    assert backend.keys("p/") == ["p/a", "p/b", "p/c"]
    listed = [request for name, request in fake.requests if name == "list_objects_v2"]
    assert listed == [
        {"Bucket": "bucket", "Prefix": "swarm/p/"},
        {"Bucket": "bucket", "Prefix": "swarm/p/", "ContinuationToken": "2"},
    ]
    assert backend.keys("none/") == []


def test_object_store_listing_is_bounded(monkeypatch):
    monkeypatch.setattr(object_store, "PAGES", 1)
    _, backend = object_backend(FakeObjectStore(page_size=2))
    for key in ("p/a", "p/b", "p/c"):
        backend.write(key, b"x")
    with pytest.raises(base.ArtifactError) as raised:
        backend.keys("p/")
    assert str(raised.value) == "listing p/ did not finish within 1 pages"


def test_truncated_copy_is_refused_and_removed(tmp_path):
    world = World("object-store", tmp_path)
    world.fake.truncate_copies = 1
    with pytest.raises(base.ArtifactError) as raised:
        world.store.put(world.scope, "a1", DATA)
    assert str(raised.value) == (
        f"object-store reported a successful write of {len(DATA)} bytes to {object_key(DATA)} "
        f"but the read back does not match {sha(DATA)}"
    )
    assert world.keys() == []
    assert world.store.metrics() == {base.METRIC: {"object-store": 1}}


def test_package_acceptance_cases_pass():
    from tests.sv2_fsy04_cases import case_a, case_b, case_c

    assert [case()["passed"] for case in (case_a, case_b, case_c)] == [True, True, True]
