from collections.abc import Mapping
from typing import Any

import boto3
import botocore.config
import botocore.session
from botocore.config import Config


def default_signer(environ: Mapping[str, str]) -> Any:
    # ruleid: lab-presign-without-sigv4
    return boto3.client(
        "s3",
        endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"],
        aws_access_key_id=environ["CI_LAB_MINIO_ACCESS_KEY"],
        aws_secret_access_key=environ["CI_LAB_MINIO_SECRET_KEY"],
    )


def path_style_only(environ: Mapping[str, str]) -> Any:
    # ruleid: lab-presign-without-sigv4
    return boto3.client(
        "s3", endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"], config=Config(s3={"addressing_style": "path"})
    )


def sigv2_by_name(environ: Mapping[str, str]) -> Any:
    # ruleid: lab-presign-without-sigv4
    return boto3.client("s3", endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"], config=Config(signature_version="s3"))


def session_default(environ: Mapping[str, str]) -> Any:
    session = botocore.session.get_session()
    # ruleid: lab-presign-without-sigv4
    return session.create_client("s3", endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"])


def resource_default(environ: Mapping[str, str]) -> Any:
    # ruleid: lab-presign-without-sigv4
    return boto3.resource("s3", endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"])


def sigv4(environ: Mapping[str, str]) -> Any:
    # ok: lab-presign-without-sigv4
    return boto3.client(
        "s3",
        endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        aws_access_key_id=environ["CI_LAB_MINIO_ACCESS_KEY"],
        aws_secret_access_key=environ["CI_LAB_MINIO_SECRET_KEY"],
    )


def session_sigv4(environ: Mapping[str, str]) -> Any:
    session = botocore.session.get_session()
    # ok: lab-presign-without-sigv4
    return session.create_client(
        "s3",
        endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"],
        config=botocore.config.Config(signature_version="s3v4"),
    )


def resource_sigv4(environ: Mapping[str, str]) -> Any:
    # ok: lab-presign-without-sigv4
    return boto3.resource("s3", endpoint_url=environ["CI_LAB_MINIO_ENDPOINT"], config=Config(signature_version="s3v4"))


def another_service() -> Any:
    # ok: lab-presign-without-sigv4
    return boto3.client("sqs")
