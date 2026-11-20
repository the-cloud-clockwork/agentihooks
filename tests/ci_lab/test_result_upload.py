"""Tests of the CI lab result upload: the scheduler's storage signs the upload URL and the worker's client puts a
unit's report to a bucket double that checks the presigned signature over the request it received, as MinIO does."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

import boto3  # pyright: ignore[reportMissingTypeStubs]
import pytest
from botocore.config import Config

from scripts.ci_lab import objects, scheduler, worker

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

BUCKET = "lab-bucket"
SIGNING = "lab"
JOB = "h1:static:static-v1-k"
REPORT = b'{"outcome": "passed"}'
REFUSED = b"<Error><Code>SignatureDoesNotMatch</Code></Error>"


def _sigv2(method: str, path: str, query: dict[str, list[str]], headers: dict[str, str]) -> bool:
    text = "\n".join(
        [method, headers.get("content-md5", ""), headers.get("content-type", ""), query["Expires"][0], path]
    )
    digest = hmac.new(SIGNING.encode(), text.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode() == query["Signature"][0]


def _sigv4(method: str, path: str, raw: str, query: dict[str, list[str]], headers: dict[str, str]) -> bool:
    signed = query["X-Amz-SignedHeaders"][0].split(";")
    scope = query["X-Amz-Credential"][0].split("/", 1)[1]
    pairs = sorted(pair.partition("=")[::2] for pair in raw.split("&") if not pair.startswith("X-Amz-Signature="))
    canonical = "\n".join(
        [
            method,
            path,
            "&".join(f"{name}={value}" for name, value in pairs),
            "".join(f"{name}:{headers[name].strip()}\n" for name in signed),
            ";".join(signed),
            "UNSIGNED-PAYLOAD",
        ]
    )
    text = "\n".join(
        ["AWS4-HMAC-SHA256", query["X-Amz-Date"][0], scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = f"AWS4{SIGNING}".encode()
    for part in scope.split("/"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest() == query["X-Amz-Signature"][0]


class Bucket:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

        def put(request: BaseHTTPRequestHandler) -> None:
            body = request.rfile.read(int(request.headers["Content-Length"]))
            parts = urlsplit(request.path)
            query = parse_qs(parts.query, keep_blank_values=True)
            headers = {name.lower(): value for name, value in request.headers.items()}
            if "X-Amz-Signature" in query:
                signed = _sigv4(request.command, parts.path, parts.query, query, headers)
            else:
                signed = "Signature" in query and _sigv2(request.command, parts.path, query, headers)
            if signed:
                self.bodies[unquote(parts.path)] = body
            reply = b"" if signed else REFUSED
            request.send_response(200 if signed else 403)
            request.send_header("Content-Length", str(len(reply)))
            request.end_headers()
            request.wfile.write(reply)

        handler = type("BucketHandler", (BaseHTTPRequestHandler,), {"do_PUT": put})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def bucket() -> Iterator[Bucket]:
    bucket = Bucket()
    threading.Thread(target=bucket.server.serve_forever, daemon=True).start()
    yield bucket
    bucket.server.shutdown()
    bucket.server.server_close()


def _report(tmp_path: Path) -> Path:
    report = tmp_path / "report.json"
    report.write_bytes(REPORT)
    return report


def _storage(bucket: Bucket) -> objects.S3Objects:
    return scheduler.storage(
        {
            scheduler.MINIO_ENDPOINT: bucket.url,
            scheduler.MINIO_ACCESS_KEY: SIGNING,
            scheduler.MINIO_SECRET_KEY: SIGNING,
            scheduler.MINIO_BUCKET: BUCKET,
        }
    )


@pytest.mark.feature("lab-result-upload-signing")
def test_a_worker_put_of_a_report_lands_through_the_upload_url_the_scheduler_storage_signs(
    bucket: Bucket, tmp_path: Path
) -> None:
    url = objects.Uploads(_storage(bucket)).upload_url(JOB, "report")

    worker.Client("http://scheduler.lab.test").put(url, _report(tmp_path))

    assert bucket.bodies == {f"/{BUCKET}/lab/uploads/{JOB}/report.json": REPORT}


@pytest.mark.feature("lab-result-upload-signing")
def test_an_upload_url_the_scheduler_storage_signs_writes_only_the_object_it_names(
    bucket: Bucket, tmp_path: Path
) -> None:
    url = objects.Uploads(_storage(bucket)).upload_url(JOB, "report")

    with pytest.raises(urllib.error.HTTPError) as refused:
        worker.Client("http://scheduler.lab.test").put(url.replace("report.json", "log.txt"), _report(tmp_path))

    assert (refused.value.code, refused.value.read()) == (403, REFUSED)
    assert bucket.bodies == {}


@pytest.mark.feature("lab-result-upload-signing")
def test_the_bucket_refuses_a_sigv2_upload_url_for_the_content_type_urllib_adds_to_the_worker_put(
    bucket: Bucket, tmp_path: Path
) -> None:
    sigv2 = boto3.client(
        "s3",
        endpoint_url=bucket.url,
        region_name="us-east-1",
        config=Config(signature_version="s3"),
        aws_access_key_id=SIGNING,
        aws_secret_access_key=SIGNING,
    )
    url = objects.Uploads(objects.S3Objects(sigv2, BUCKET, "")).upload_url(JOB, "report")

    with pytest.raises(urllib.error.HTTPError) as refused:
        worker.Client("http://scheduler.lab.test").put(url, _report(tmp_path))
    split = urlsplit(url)
    connection = http.client.HTTPConnection(split.netloc, timeout=10)
    connection.request("PUT", f"{split.path}?{split.query}", body=REPORT)
    accepted = connection.getresponse().status
    connection.close()

    assert (refused.value.code, refused.value.read()) == (403, REFUSED)
    assert accepted == 200
    assert bucket.bodies == {f"/{BUCKET}/lab/uploads/{JOB}/report.json": REPORT}
