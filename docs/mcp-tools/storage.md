---
title: Storage
nav_order: 7
parent: MCP Tools
---

# Storage Tools
{: .no_toc }

The Storage category provides S3 uploads for session artifacts.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Tools

| Tool | Description |
|------|-------------|
| `storage_upload_path()` | Upload a file or directory to S3 |

---

## Tool reference

### `storage_upload_path`

```python
storage_upload_path(
    session_id: str,
    path: str,
    prefix: str = "",
    match_uuid: bool = False
) -> str
```

Uploads a file or entire directory to S3 under the key prefix `sessions/<session_id>/`. An optional `prefix` is appended after the session path. When `match_uuid=True`, only uploads files whose names contain a UUID pattern.

**Returns:** JSON with `success` (bool), `s3_url`, `files_uploaded`, `error`

---

## Notes

### S3 path structure

Uploaded files are stored at:

```
s3://<bucket>/sessions/<session_id>/<prefix>/<filename>
```

The `STORAGE_URL` environment variable determines the bucket and endpoint.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `STORAGE_URL` | Yes | — | S3 URL or endpoint (e.g., `s3://my-bucket`) |
| `IS_EVALUATION` | No | `false` | Evaluation mode flag (skips actual upload) |
