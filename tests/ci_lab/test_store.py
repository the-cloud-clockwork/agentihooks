from __future__ import annotations

import dataclasses
import io
import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

import botocore.session
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from scripts.ci_lab import digests, lease_api, objects, store

if TYPE_CHECKING:
    import fakeredis

BUCKET = "lab-bucket"


class Bucket:
    """An in memory S3 client: conditional puts, whole reads and one page listings."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, IfNoneMatch: str) -> None:
        assert (Bucket, IfNoneMatch) == (BUCKET, "*")
        if Key in self.blobs:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.blobs[Key] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == BUCKET
        return {"Body": io.BytesIO(self.blobs[Key])}

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict[str, Any]:
        assert Bucket == BUCKET
        return {"Contents": [{"Key": name} for name in self.blobs if name.startswith(Prefix)], "IsTruncated": False}

    def delete_object(self, Bucket: str, Key: str) -> None:
        assert Bucket == BUCKET
        self.blobs.pop(Key, None)


def key(kind: str, seed: str) -> str:
    return f"{kind}-v1-{digests.digest({'seed': seed})}"


def hexdigest(unit_key: str) -> str:
    return unit_key.rsplit("-", 1)[1]


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture(params=["local", "s3"])
def blobs(request: pytest.FixtureRequest, tmp_path: Path) -> objects.Objects:
    if request.param == "local":
        return objects.LocalObjects(tmp_path)
    return objects.S3Objects(Bucket(), BUCKET, "ci/")


@pytest.fixture
def verdicts(client: fakeredis.FakeRedis, blobs: objects.Objects) -> store.Store:
    return store.Store(client, blobs)


def test_put_stores_the_verdict_report_and_log_and_get_returns_it(
    verdicts: store.Store, client: fakeredis.FakeRedis
) -> None:
    unit = key("test", "a")
    verdict = store.Verdict(kind="test", key=unit, passed=True, flaky=True, seconds=1.5)

    assert verdicts.put(verdict, {"tests": 2, "failed": []}, b"2 passed\n") is True

    assert verdicts.get("test", unit) == verdict
    assert client.keys("*") == [f"agentihooks-ci:verdict:test:{unit}"]
    assert client.get(f"agentihooks-ci:verdict:test:{unit}") == (
        f'{{"flaky": true, "key": "{unit}", "kind": "test", "passed": true, "seconds": 1.5}}'
    )
    folder = f"lab/test/{hexdigest(unit)}/"
    assert verdicts.objects.keys("lab/") == [f"{folder}log.txt", f"{folder}report.json"]
    assert verdicts.objects.read(f"{folder}log.txt") == b"2 passed\n"
    assert json.loads(verdicts.objects.read(f"{folder}report.json")) == {
        "report": {"failed": [], "tests": 2},
        "verdict": {"flaky": True, "key": unit, "kind": "test", "passed": True, "seconds": 1.5},
    }
    assert verdicts.objects.read(f"{folder}report.json") == (
        b'{"report": {"failed": [], "tests": 2}, "verdict": {"flaky": true, "key": "'
        + unit.encode()
        + b'", "kind": "test", "passed": true, "seconds": 1.5}}'
    )


def test_a_second_put_is_a_no_op(verdicts: store.Store) -> None:
    unit = key("mutant", "f")
    first = store.Verdict(kind="mutant", key=unit, passed=True, flaky=False, seconds=4.0)
    second = store.Verdict(kind="mutant", key=unit, passed=False, flaky=False, seconds=9.0)
    folder = f"lab/mutant/{hexdigest(unit)}/"
    verdicts.put(first, {"killed": 3}, b"first\n")
    report = verdicts.objects.read(f"{folder}report.json")

    assert verdicts.put(second, {"killed": 0}, b"second\n") is False

    assert verdicts.get("mutant", unit) == first
    assert verdicts.objects.read(f"{folder}report.json") == report
    assert verdicts.objects.read(f"{folder}log.txt") == b"first\n"


def test_a_second_put_never_rewrites_an_existing_index_entry(
    verdicts: store.Store, client: fakeredis.FakeRedis
) -> None:
    unit = key("test", "kept")
    verdict = store.Verdict(kind="test", key=unit, passed=True, flaky=False, seconds=1.0)
    verdicts.put(verdict, {}, b"")
    client.set(f"agentihooks-ci:verdict:test:{unit}", "held")

    assert verdicts.put(verdict, {}, b"") is False

    assert client.get(f"agentihooks-ci:verdict:test:{unit}") == "held"


def test_a_put_after_the_index_is_lost_keeps_the_first_verdict(
    verdicts: store.Store, client: fakeredis.FakeRedis
) -> None:
    unit = key("static", "ruff")
    first = store.Verdict(kind="static", key=unit, passed=False, flaky=False, seconds=2.0)
    verdicts.put(first, {"errors": 1}, b"E501\n")
    client.flushall()

    assert verdicts.put(store.Verdict(kind="static", key=unit, passed=True, flaky=False, seconds=1.0), {}, b"") is False

    assert verdicts.get("static", unit) == first


def test_a_put_after_the_objects_are_lost_rewrites_them_and_the_index(
    tmp_path: Path, client: fakeredis.FakeRedis
) -> None:
    verdicts = store.Store(client, objects.LocalObjects(tmp_path))
    unit = key("test", "lost")
    later = store.Verdict(kind="test", key=unit, passed=False, flaky=False, seconds=5.0)
    verdicts.put(store.Verdict(kind="test", key=unit, passed=True, flaky=False, seconds=1.0), {}, b"one\n")
    shutil.rmtree(tmp_path / "lab")

    assert verdicts.put(later, {"failed": ["t"]}, b"two\n") is True

    assert verdicts.get("test", unit) == later
    assert verdicts.objects.read(f"lab/test/{hexdigest(unit)}/log.txt") == b"two\n"


def test_a_put_that_finds_a_report_without_a_log_never_adds_a_log_of_another_run(
    verdicts: store.Store,
) -> None:
    unit = key("test", "crashed")
    first = store.Verdict(kind="test", key=unit, passed=True, flaky=False, seconds=1.0)
    folder = f"lab/test/{hexdigest(unit)}/"
    document = {"report": {}, "verdict": dataclasses.asdict(first)}
    verdicts.objects.write_once(f"{folder}report.json", json.dumps(document).encode())

    assert verdicts.put(store.Verdict(kind="test", key=unit, passed=False, flaky=False, seconds=2.0), {}, b"x") is False

    assert verdicts.get("test", unit) == first
    assert verdicts.objects.keys("lab/") == [f"{folder}report.json"]


def test_get_after_a_redis_flush_plus_reindex_returns_the_verdict(
    verdicts: store.Store, client: fakeredis.FakeRedis
) -> None:
    passed = store.Verdict(kind="test", key=key("test", "a"), passed=True, flaky=True, seconds=3.0)
    failed = store.Verdict(kind="mutant", key=key("mutant", "f"), passed=False, flaky=False, seconds=7.25)
    verdicts.put(passed, {"tests": 1}, b"ok\n")
    verdicts.put(failed, {"survived": ["x"]}, b"survived\n")
    verdicts.objects.write_once(f"{objects.grade_prefix('77')}report.json", b"{}")
    verdicts.objects.write_once(objects.upload_path("h1:test:k", "report"), b"{}")
    client.flushall()
    assert verdicts.get("test", passed.key) is None

    assert verdicts.reindex() == 2

    assert verdicts.get("test", passed.key) == passed
    assert verdicts.get("mutant", failed.key) == failed
    assert sorted(client.keys("*")) == sorted(
        [f"agentihooks-ci:verdict:test:{passed.key}", f"agentihooks-ci:verdict:mutant:{failed.key}"]
    )
    assert verdicts.reindex() == 0


def test_a_disabled_kind_always_misses(verdicts: store.Store, client: fakeredis.FakeRedis) -> None:
    tests = store.Verdict(kind="test", key=key("test", "a"), passed=True, flaky=False, seconds=1.0)
    static = store.Verdict(kind="static", key=key("static", "a"), passed=True, flaky=False, seconds=1.0)
    verdicts.put(tests, {}, b"")
    verdicts.put(static, {}, b"")

    verdicts.disable("test")
    late = store.Verdict(kind="test", key=key("test", "b"), passed=True, flaky=False, seconds=2.0)

    assert client.smembers("agentihooks-ci:disabled") == {"test"}
    assert verdicts.get("test", tests.key) is None
    assert verdicts.put(late, {}, b"") is True
    assert verdicts.get("test", late.key) is None
    assert verdicts.get("static", static.key) == static

    verdicts.enable("test")

    assert client.smembers("agentihooks-ci:disabled") == set()
    assert verdicts.get("test", tests.key) == tests
    assert verdicts.get("test", late.key) == late


def test_a_negative_verdict_hits_unless_the_caller_bypasses_negatives(verdicts: store.Store) -> None:
    failed = store.Verdict(kind="test", key=key("test", "red"), passed=False, flaky=False, seconds=1.0)
    passed = store.Verdict(kind="test", key=key("test", "green"), passed=True, flaky=False, seconds=1.0)
    verdicts.put(failed, {"failed": ["t"]}, b"1 failed\n")
    verdicts.put(passed, {"failed": []}, b"1 passed\n")

    assert verdicts.get("test", failed.key) == failed
    assert verdicts.get("test", failed.key, bypass_negative=True) is None
    assert verdicts.get("test", passed.key, bypass_negative=True) == passed
    assert verdicts.get("test", key("test", "unknown")) is None


def test_a_flaky_verdict_is_a_pass() -> None:
    unit = key("test", "flake")

    with pytest.raises(ValueError, match=f"^a flaky verdict is a pass: {unit}$"):
        store.Verdict(kind="test", key=unit, passed=False, flaky=True, seconds=1.0)
    assert store.Verdict(kind="test", key=unit, passed=True, flaky=True, seconds=1.0).flaky is True


def test_put_refuses_a_key_of_another_kind_or_shape(verdicts: store.Store) -> None:
    other = key("mutant", "f")

    with pytest.raises(ValueError, match=f"^not a test unit key: {other}$"):
        verdicts.put(store.Verdict(kind="test", key=other, passed=True, flaky=False, seconds=0.0), {}, b"")
    digest = hexdigest(key("test", "a"))
    for bad in ("test-v1-abc", key("test", "a") + "x", "test-v1-" + "g" * 64, "test-" + digest, "test-v-" + digest):
        with pytest.raises(ValueError, match=f"^not a test unit key: {bad}$"):
            verdicts.put(store.Verdict(kind="test", key=bad, passed=True, flaky=False, seconds=0.0), {}, b"")
    assert verdicts.objects.keys("lab/") == []


def test_the_layout_follows_the_module_table() -> None:
    unit = key("stats", "s")

    assert objects.unit_path("stats", unit, "report") == f"lab/stats/{hexdigest(unit)}/report.json"
    assert objects.unit_path("stats", unit, "log") == f"lab/stats/{hexdigest(unit)}/log.txt"
    assert objects.grade_prefix("1234") == "lab/grades/1234/"
    assert objects.upload_path("h1:test:k", "report") == "lab/uploads/h1:test:k/report.json"
    assert objects.upload_path("h1:test:k", "log") == "lab/uploads/h1:test:k/log.txt"
    assert objects.is_report(f"lab/stats/{hexdigest(unit)}/report.json") is True
    assert objects.is_report(f"lab/stats/{hexdigest(unit)}/log.txt") is False
    assert objects.is_report("lab/grades/1234/report.json") is False
    assert objects.is_report(f"lab/stats/{hexdigest(unit)}/report.jsonx") is False
    assert objects.is_report(f"lab/stats/{hexdigest(unit)}/reportxjson") is False
    assert objects.is_report("lab/stats/" + "g" * 64 + "/report.json") is False


def test_local_objects_write_once_and_leave_no_staging_file(tmp_path: Path) -> None:
    local = objects.LocalObjects(tmp_path)

    assert local.write_once("lab/a/report.json", b"first") is True
    assert local.write_once("lab/a/report.json", b"second") is False

    assert local.read("lab/a/report.json") == b"first"
    assert sorted(path.name for path in (tmp_path / "lab" / "a").iterdir()) == ["report.json"]
    assert local.keys("lab/") == ["lab/a/report.json"]


def test_s3_objects_live_under_the_bucket_prefix() -> None:
    bucket = Bucket()
    remote = objects.S3Objects(bucket, BUCKET, "ci/")

    assert remote.write_once("lab/a/log.txt", b"one") is True
    assert remote.write_once("lab/a/log.txt", b"two") is False

    assert bucket.blobs == {"ci/lab/a/log.txt": b"one"}
    assert remote.read("lab/a/log.txt") == b"one"
    assert remote.keys("lab/") == ["lab/a/log.txt"]


@pytest.mark.feature("lab-outside-notes-per-commit")
def test_objects_remove_one_path_and_take_a_missing_one_as_gone(blobs: objects.Objects) -> None:
    blobs.write_once("lab/a/report.json", b"one")
    blobs.write_once("lab/b/report.json", b"two")

    blobs.remove("lab/a/report.json")
    blobs.remove("lab/a/report.json")

    assert blobs.keys("lab/") == ["lab/b/report.json"]
    assert blobs.read("lab/b/report.json") == b"two"


def s3_client() -> Any:
    session = botocore.session.get_session()
    session.set_credentials("lab", "lab")
    return session.create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url="http://minio.lab.test:9000",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def test_uploads_mint_presigned_put_urls_for_a_jobs_report_and_log() -> None:
    signer: lease_api.UploadSigner = objects.Uploads(objects.S3Objects(s3_client(), BUCKET, "ci/"), expires_s=600)

    url = urlsplit(signer.upload_url("h1:test:k", "log"))
    query = parse_qs(url.query)

    assert (url.scheme, url.netloc) == ("http", "minio.lab.test:9000")
    assert unquote(url.path) == "/lab-bucket/ci/lab/uploads/h1:test:k/log.txt"
    assert query["X-Amz-Expires"] == ["600"]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert re.fullmatch("[0-9a-f]{64}", query["X-Amz-Signature"][0])


def test_uploads_expire_after_fifteen_minutes_by_default() -> None:
    url = objects.Uploads(objects.S3Objects(s3_client(), BUCKET, "")).upload_url("h1:test:k", "report")
    query = parse_qs(urlsplit(url).query)

    assert unquote(urlsplit(url).path) == "/lab-bucket/lab/uploads/h1:test:k/report.json"
    assert query["X-Amz-Expires"] == ["900"]


def test_local_uploads_name_the_file_the_worker_writes(tmp_path: Path) -> None:
    url = objects.Uploads(objects.LocalObjects(tmp_path)).upload_url("h1:test:k", "report")

    assert url == (tmp_path.resolve() / "lab" / "uploads" / "h1:test:k" / "report.json").as_uri()


@pytest.mark.feature("lab-flakes-object-once")
def test_flakes_path_names_the_unit_flakes_object_and_refuses_a_key_of_another_kind() -> None:
    unit = key("test", "a")
    path = objects.flakes_path("test", unit)

    assert path == f"lab/flakes/test/{hexdigest(unit)}.jsonl"
    assert not objects.is_report(path)
    for bad in (key("mutant", "f"), "test-v1-abc", unit + "x", "test-v-" + hexdigest(unit)):
        with pytest.raises(ValueError, match=f"^not a test unit key: {bad}$"):
            objects.flakes_path("test", bad)


@pytest.mark.feature("lab-flakes-object-once")
def test_reindex_counts_only_reports_beside_a_flakes_object(verdicts: store.Store, client: fakeredis.FakeRedis) -> None:
    flaky = store.Verdict(kind="test", key=key("test", "a"), passed=True, flaky=True, seconds=3.0)
    flakes = f"lab/flakes/test/{hexdigest(flaky.key)}.jsonl"
    verdicts.put(flaky, {"flaky": True}, b"ok\n")
    verdicts.objects.write_once(flakes, b'{"test": "t", "outcome": "flaky"}\n')
    client.flushall()

    assert verdicts.reindex() == 1

    assert client.keys("*") == [f"agentihooks-ci:verdict:test:{flaky.key}"]
    assert verdicts.get("test", flaky.key) == flaky
    assert verdicts.objects.read(flakes) == b'{"test": "t", "outcome": "flaky"}\n'
