"""Raw uploaded bytes for the Company Brain — S3 in production, disk in dev.

The durable truth for RETRIEVAL is the extracted text in Postgres
(knowledge_document_versions); this adapter only keeps the original file so a
future re-extraction (better parser, higher caps) never needs a re-upload.

With KNOWLEDGE_BUCKET set the adapter uses S3 through boto3 and the ambient
IAM role/credentials — keys never live in git or logs. Without it, files land
in a directory next to the SQLite store, which keeps the whole feature
key-free for local dev and tests. ``storage_ref`` strings are opaque to every
caller ("s3://bucket/key" or "file:<relative path>").
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..config import settings

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", (name or "file").strip())[:120]
    return cleaned or "file"


def _local_root() -> Path:
    from .. import store

    return store.STORE_PATH.parent / "knowledge_files"


def _s3_client():
    try:
        import boto3
    except ImportError as e:  # config error — fail loudly, never mask
        raise RuntimeError(
            "KNOWLEDGE_BUCKET is set but boto3 is not installed: "
            "pip install boto3"
        ) from e
    kwargs: dict = {"region_name": settings.knowledge_aws_region}
    if settings.knowledge_s3_endpoint.strip():
        kwargs["endpoint_url"] = settings.knowledge_s3_endpoint.strip()
    return boto3.client("s3", **kwargs)


def put_bytes(org_id: str, document_id: str, filename: str, data: bytes) -> str:
    """Persist one uploaded file; returns the opaque storage_ref."""
    name = _safe_name(filename)
    bucket = settings.knowledge_bucket.strip()
    if bucket:
        key = f"knowledge/{org_id}/{document_id}/{name}"
        _s3_client().put_object(Bucket=bucket, Key=key, Body=data)
        return f"s3://{bucket}/{key}"
    rel = Path(org_id) / document_id / name
    path = _local_root() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"file:{rel.as_posix()}"


def get_bytes(storage_ref: str) -> Optional[bytes]:
    """The stored bytes for a ref, or None when unavailable."""
    ref = (storage_ref or "").strip()
    if ref.startswith("s3://"):
        rest = ref[len("s3://"):]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            return None
        try:
            obj = _s3_client().get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()
        except Exception:  # noqa: BLE001 — the job records a distilled error
            return None
    if ref.startswith("file:"):
        path = _local_root() / ref[len("file:"):]
        try:
            return path.read_bytes()
        except OSError:
            return None
    return None
