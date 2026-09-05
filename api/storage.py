"""Cloudflare R2 storage for the generated report PDFs.

R2 is S3-compatible, so this is boto3 pointed at the R2 endpoint. Objects are
laid out as ``insider/reports/<snapshot_date>/<report_id>/<filename>`` and
handed to the browser as short-lived presigned URLs, so the bucket itself
stays private.

The bucket is shared with the other two services. They write under ``jobs/``,
``packs/`` and ``seed/``; everything here lives under the ``insider/`` prefix,
so nothing can collide.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

import boto3
from botocore.config import Config

from api.settings import DOWNLOAD_URL_TTL_SECONDS, R2_PREFIX

_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".png": "image/png",
    ".csv": "text/csv",
    ".json": "application/json",
}


def _client():
    """Create a boto3 S3 client configured for Cloudflare R2."""
    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _bucket() -> str:
    return os.getenv("R2_BUCKET_NAME", "tradingagents")


def upload_report_file(snapshot_date: str, report_id: str, file_path: Path) -> str:
    """Upload one report artifact and return its R2 object key."""
    key = f"{R2_PREFIX}/reports/{snapshot_date}/{report_id}/{file_path.name}"
    content_type = _CONTENT_TYPES.get(file_path.suffix.lower(), "application/octet-stream")

    _client().upload_file(
        str(file_path),
        _bucket(),
        key,
        ExtraArgs={"ContentType": content_type},
    )
    return key


def object_exists(key: str) -> bool:
    """True if an object is present in the bucket."""
    from botocore.exceptions import ClientError

    try:
        _client().head_object(Bucket=_bucket(), Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_file(key: str, source: Path) -> int:
    """Upload a local file under an explicit key. Returns its size in bytes."""
    content_type = _CONTENT_TYPES.get(source.suffix.lower(), "application/octet-stream")
    _client().upload_file(
        str(source), _bucket(), key, ExtraArgs={"ContentType": content_type}
    )
    return source.stat().st_size


def download_to_file(key: str, dest: Path) -> int:
    """Download an object to a local path. Returns its size in bytes.

    Writes to a temporary file first so an interrupted transfer cannot leave
    a truncated file that later looks complete.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    _client().download_file(_bucket(), key, str(tmp))
    tmp.replace(dest)
    return dest.stat().st_size


def get_download_url(key: str, filename: str | None = None) -> str:
    """Presigned GET URL for an R2 object.

    ``filename`` sets a Content-Disposition so the browser saves the file
    under a friendly name rather than the full object key.
    """
    params: Dict[str, str] = {"Bucket": _bucket(), "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'

    return _client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=DOWNLOAD_URL_TTL_SECONDS,
    )
