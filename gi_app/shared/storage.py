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
S3_PUBLIC_ENDPOINT = os.environ.get("S3_PUBLIC_ENDPOINT") or None
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.environ.get("S3_BUCKET", "gi-cancer")

# Real AWS S3 is an optional second target (the download tool can send a dataset here
# instead of the local MinIO). Unset AWS_S3_ENDPOINT = real AWS S3.
AWS_S3_ENDPOINT = os.environ.get("AWS_S3_ENDPOINT") or None
AWS_S3_ACCESS_KEY = os.environ.get("AWS_S3_ACCESS_KEY")
AWS_S3_SECRET_KEY = os.environ.get("AWS_S3_SECRET_KEY")
AWS_S3_BUCKET = os.environ.get("AWS_S3_BUCKET")
AWS_S3_REGION = os.environ.get("AWS_S3_REGION")

# Named storage targets. "local" = in-stack MinIO (always available); "aws" = real S3.
_PROFILES = {
    "local": {"endpoint": S3_ENDPOINT, "key": S3_ACCESS_KEY, "secret": S3_SECRET_KEY,
              "bucket": S3_BUCKET, "region": None},
    "aws": {"endpoint": AWS_S3_ENDPOINT, "key": AWS_S3_ACCESS_KEY, "secret": AWS_S3_SECRET_KEY,
            "bucket": AWS_S3_BUCKET, "region": AWS_S3_REGION},
}

_clients = {}
_presign = None


def is_target_configured(target: str = "local") -> bool:
    """Whether a storage target is usable (local MinIO always is; aws needs creds + bucket)."""
    if target == "local":
        return True
    p = _PROFILES.get(target)
    return bool(p and p["key"] and p["secret"] and p["bucket"])


def bucket_for(target: str = "local") -> str:
    """The bucket configured for a storage target."""
    return _PROFILES.get(target, {}).get("bucket") or S3_BUCKET


def target_for_uri(uri: str) -> str:
    """Infer which target an s3:// URI lives on (by bucket name), defaulting to local."""
    bucket, _ = parse_s3_uri(uri)
    if AWS_S3_BUCKET and bucket == AWS_S3_BUCKET:
        return "aws"
    return "local"


def client(target: str = "local"):
    """Build (and cache) the boto3 S3 client for a storage target ("local" | "aws").

    Args:
        target: Which storage profile to use.

    Returns:
        A boto3 S3 client configured from that profile's env vars.
    """
    if target not in _clients:
        p = _PROFILES[target]
        kwargs = dict(
            endpoint_url=p["endpoint"],
            aws_access_key_id=p["key"],
            aws_secret_access_key=p["secret"],
            config=Config(signature_version="s3v4"),
        )
        if p.get("region"):
            kwargs["region_name"] = p["region"]
        _clients[target] = boto3.client("s3", **kwargs)
    return _clients[target]


def presign_client():
    """Build (and cache) the client used to sign browser-facing download URLs.

    SigV4 presigned URLs bind the request host into the signature, so a link the
    browser opens must be signed against the *public* endpoint (e.g.
    http://localhost:9000), not the in-cluster one (http://minio:9000) — otherwise
    MinIO recomputes the signature from the received Host header and returns
    SignatureDoesNotMatch. When S3_PUBLIC_ENDPOINT is unset (real AWS S3, or a
    setup where the signing and browser hosts already match) we reuse the normal
    client.

    Returns:
        A boto3 S3 client whose presigned URLs are valid from the browser.
    """
    global _presign
    if not S3_PUBLIC_ENDPOINT:
        return client()
    if _presign is None:
        _presign = boto3.client(
            "s3",
            endpoint_url=S3_PUBLIC_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
    return _presign


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


def build_uri(*parts: str, bucket: str = None, target: str = "local") -> str:
    """Join key parts into an s3:// URI for a storage target.

    Args:
        *parts: Path segments to join, e.g. 'bronze', 'TCGA-COAD', 'slides', 'x.svs'.
        bucket: Bucket name; defaults to the target's configured bucket.
        target: Storage target ("local" | "aws") whose bucket to use.

    Returns:
        The full URI, e.g. 's3://gi-cancer/bronze/TCGA-COAD/slides/x.svs'.
    """
    key = "/".join(p.strip("/") for p in parts if p)
    return f"s3://{bucket or bucket_for(target)}/{key}"


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


def put_file(local_path: str, uri: str, target: str = "local") -> None:
    """Upload a local file to object storage.

    Args:
        local_path: Path to the file on disk.
        uri: Destination s3:// URI.
        target: Storage target ("local" | "aws").

    Returns:
        None.
    """
    bucket, key = parse_s3_uri(uri)
    client(target).upload_file(local_path, bucket, key)


def delete_object(uri: str, target: str = None) -> None:
    """Delete an object from storage (idempotent — no error if it's already gone).

    Args:
        uri: The object's s3:// URI.
        target: Storage target; inferred from the URI's bucket when omitted.

    Returns:
        None.
    """
    bucket, key = parse_s3_uri(uri)
    client(target or target_for_uri(uri)).delete_object(Bucket=bucket, Key=key)


def download_file(uri: str, local_path: str, target: str = None) -> None:
    """Download an object from storage to a local path.

    Args:
        uri: The object's s3:// URI.
        local_path: Destination path on the local filesystem.
        target: Storage target; inferred from the URI's bucket when omitted.

    Returns:
        None.
    """
    bucket, key = parse_s3_uri(uri)
    client(target or target_for_uri(uri)).download_file(bucket, key, local_path)


def url_for(uri: str, expires: int = 3600, content_type: str = None,
            inline: bool = False) -> str:
    """Make a temporary download link for an object.

    Objects are uploaded without a declared content type, so MinIO serves them as
    binary/octet-stream and a browser downloads them instead of rendering. Passing
    `content_type` (and `inline`) overrides that per-link via the response-header
    parameters, which are part of the signature — so this cannot be tampered with
    after signing.

    Args:
        uri: The object's s3:// URI.
        expires: Link lifetime in seconds (default 3600).
        content_type: Override the Content-Type header, e.g. 'application/pdf'.
        inline: Ask the browser to display rather than download.

    Returns:
        A presigned GET URL valid for `expires` seconds.
    """
    bucket, key = parse_s3_uri(uri)
    params = {"Bucket": bucket, "Key": key}
    if content_type:
        params["ResponseContentType"] = content_type
    if inline:
        params["ResponseContentDisposition"] = "inline"
    return presign_client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires
    )
