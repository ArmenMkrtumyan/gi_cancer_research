"""Object-storage abstraction over an S3-compatible endpoint.

Locally this points at MinIO; in AWS set S3_ENDPOINT to the S3 endpoint (or leave it
unset for real S3) and the same code path is used. Everything is addressed by `s3://`
URIs so callers never hardcode a backend.
"""

import os
from urllib.parse import urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

S3_ENDPOINT = os.environ.get("S3_ENDPOINT") or None
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.environ.get("S3_BUCKET", "gi-cancer")

_client = None


def client():
    """Build (and cache) the boto3 S3 client.

    Returns:
        The shared boto3 S3 client, configured from the S3_* environment variables.
    """
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            config=Config(signature_version="s3v4"),
        )
    return _client


def parse_s3_uri(uri: str):
    """Split an s3:// URI into its bucket and key.

    Args:
        uri: An address like 's3://bucket/key/parts'.

    Returns:
        A (bucket, key) tuple, e.g. ('bucket', 'key/parts').
    """
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        raise ValueError(f"Not an s3:// URI: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def build_uri(*parts: str, bucket: str = None) -> str:
    """Join key parts into an s3:// URI.

    Args:
        *parts: Path segments to join, e.g. 'bronze', 'TCGA-COAD', 'slides', 'x.svs'.
        bucket: Bucket name; defaults to the configured S3_BUCKET.

    Returns:
        The full URI, e.g. 's3://gi-cancer/bronze/TCGA-COAD/slides/x.svs'.
    """
    key = "/".join(p.strip("/") for p in parts if p)
    return f"s3://{bucket or S3_BUCKET}/{key}"


def exists(uri: str) -> bool:
    """Check whether an object exists.

    Args:
        uri: The object's s3:// URI.

    Returns:
        True if the object exists, False otherwise.
    """
    bucket, key = parse_s3_uri(uri)
    try:
        client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def put_file(local_path: str, uri: str) -> None:
    """Upload a local file to object storage.

    Args:
        local_path: Path to the file on disk.
        uri: Destination s3:// URI.

    Returns:
        None.
    """
    bucket, key = parse_s3_uri(uri)
    client().upload_file(local_path, bucket, key)


def url_for(uri: str, expires: int = 3600) -> str:
    """Make a temporary download link for an object.

    Args:
        uri: The object's s3:// URI.
        expires: Link lifetime in seconds (default 3600).

    Returns:
        A presigned GET URL valid for `expires` seconds.
    """
    bucket, key = parse_s3_uri(uri)
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires
    )
