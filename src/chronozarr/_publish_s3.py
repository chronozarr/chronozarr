"""`StorageAdapter` for AWS S3 and S3-compatible services (Cloudflare R2) on boto3.

R2 is not a second code path. It differs from S3 in three facts, all handled here by the endpoint
URL: the region is `auto`, the endpoint (`https://<account>.r2.cloudflarestorage.com`) is
authenticated and never a browser address, and boto3's default request checksums are switched to
"when required" because S3-compatible services are not all able to verify them.

Credentials come only from boto3's own chain (environment, shared config, SSO, instance role) and
an optional named profile. For R2, put the API token's key pair in a profile or in
AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. This module never reads, prints or stores them.
"""

from __future__ import annotations

import copy
import urllib.parse
from pathlib import Path
from typing import Any

from chronozarr.publish import PublishError, RemoteObject

R2_SUFFIX = ".r2.cloudflarestorage.com"
MAX_POOL_CONNECTIONS = 64

# The viewer's requirements, in S3's CORS format (the same rule as deploy/r2-cors.json).
VIEWER_CORS_RULE: dict[str, Any] = {
    "AllowedOrigins": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["Range", "If-Match", "If-None-Match", "Content-Type"],
    "ExposeHeaders": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
    "MaxAgeSeconds": 86400,
}

_NEEDS = {
    "list": "s3:ListBucket",
    "put": "s3:PutObject",
    "get": "s3:GetObject",
    "head": "s3:ListBucket",
    "read-cors": "s3:GetBucketCORS",
    "write-cors": "s3:PutBucketCORS",
}


class S3Error(PublishError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class S3Adapter:
    """One bucket on AWS S3, or on an S3-compatible service when `endpoint_url` is set."""

    def __init__(
        self,
        bucket: str,
        *,
        profile: str | None = None,
        endpoint_url: str | None = None,
        region: str | None = None,
    ):
        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import ProfileNotFound
        except ImportError as exc:
            raise PublishError(
                "publishing to s3:// needs boto3: run `uv sync --extra publish` "
                "(or `pip install 'chronozarr[publish]'`)."
            ) from exc
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.host = ""
        if endpoint_url:
            parts = urllib.parse.urlsplit(endpoint_url)
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise PublishError(
                    "--endpoint-url must be an https URL without credentials, for example "
                    "https://<account id>.r2.cloudflarestorage.com."
                )
            self.host = parts.hostname
        self.is_r2 = self.host.endswith(R2_SUFFIX)
        self.label = (
            "Cloudflare R2"
            if self.is_r2
            else "S3-compatible storage"
            if endpoint_url
            else "AWS S3"
        )
        options: dict[str, Any] = {
            "retries": {"max_attempts": 5, "mode": "standard"},
            "max_pool_connections": MAX_POOL_CONNECTIONS,
        }
        if endpoint_url:
            options["request_checksum_calculation"] = "when_required"
            options["response_checksum_validation"] = "when_required"
        try:
            session = boto3.Session(profile_name=profile)
        except ProfileNotFound as exc:
            raise PublishError(f"AWS profile {profile!r} not found: {exc}") from exc
        self.client = session.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region or ("auto" if self.is_r2 else None),
            config=Config(**options),
        )

    def endpoint_description(self) -> str:
        if self.endpoint_url:
            return f"{self.label} endpoint {self.host} (authenticated, not a browser address)"
        return f"{self.label}, region {self.client.meta.region_name}"

    def default_public_url(self, prefix: str) -> str | None:
        """AWS only: the bucket's regional HTTPS endpoint (public only if the bucket is)."""
        if self.endpoint_url:
            return None
        response = self._call("head", self.client.head_bucket, Bucket=self.bucket)
        region = response["ResponseMetadata"]["HTTPHeaders"].get("x-amz-bucket-region")
        region = region or self.client.meta.region_name
        path = urllib.parse.quote(prefix, safe="/")
        if "." in self.bucket:  # a dotted name does not match the wildcard certificate
            return f"https://s3.{region}.amazonaws.com/{self.bucket}" + (
                f"/{path}" if path else ""
            )
        return f"https://{self.bucket}.s3.{region}.amazonaws.com" + (f"/{path}" if path else "")

    def list_objects(self, prefix: str) -> dict[str, RemoteObject]:
        found: dict[str, RemoteObject] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=self.bucket, Prefix=f"{prefix}/" if prefix else "")
        for page in self._iterate("list", pages):
            for obj in page.get("Contents", []):
                found[obj["Key"]] = RemoteObject(obj["Size"], obj["ETag"].strip('"').lower())
        return found

    def put_object(self, key: str, path: Path, *, cache_control: str, content_type: str) -> None:
        with path.open("rb") as body:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                CacheControl=cache_control,
                ContentType=content_type,
            )

    def read_object(self, key: str) -> bytes | None:
        try:
            response = self._call("get", self.client.get_object, Bucket=self.bucket, Key=key)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                return None
            raise
        with response["Body"] as body:
            return body.read()

    def read_cors(self) -> list[dict[str, Any]]:
        try:
            response = self._call("read-cors", self.client.get_bucket_cors, Bucket=self.bucket)
        except S3Error as exc:
            if exc.code == "NoSuchCORSConfiguration":
                return []
            raise
        return list(response["CORSRules"])

    def write_cors(self, rules: list[dict[str, Any]]) -> None:
        self._call(
            "write-cors",
            self.client.put_bucket_cors,
            Bucket=self.bucket,
            CORSConfiguration={"CORSRules": rules},
        )

    def cors_rule(self) -> dict[str, Any]:
        return copy.deepcopy(VIEWER_CORS_RULE)

    def access_help(self) -> str:
        if self.is_r2:
            return (
                "The objects are stored but browsers cannot read them from the public URL. The R2 "
                "endpoint used for the upload is authenticated and never a browser address. "
                "Serve the bucket through a custom domain (dashboard: R2 > bucket > Settings > "
                "Custom Domains) or enable its public r2.dev URL, then pass that address as "
                "--public-url. CORS on R2 can be set with a token that has bucket admin "
                "permission: `npx wrangler r2 bucket cors set BUCKET --file deploy/r2-cors.json`. "
                "docs/hosting-providers.md (Cloudflare R2) has the cache rules."
            )
        return (
            "The objects are stored but browsers cannot read them from the public URL. A bucket "
            "that blocks public access serves nothing to the web: chronozarr never changes public "
            "access. Either serve the bucket through CloudFront with an origin access control "
            "(docs/hosting-providers.md, Amazon S3 with CloudFront) and pass the distribution "
            "URL as --public-url, or "
            "change the bucket's public access block and policy yourself. CORS alone does not "
            "make objects readable."
        )

    def _iterate(self, action: str, pages: Any) -> Any:
        """Iterate a paginator, turning provider errors into `PublishError`."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            yield from pages
        except ClientError as exc:
            raise self._translate(action, exc) from exc
        except BotoCoreError as exc:
            raise PublishError(f"{self.label} {action} failed: {exc}") from exc

    def _call(self, action: str, method: Any, **kwargs: Any) -> Any:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return method(**kwargs)
        except ClientError as exc:
            raise self._translate(action, exc) from exc
        except BotoCoreError as exc:
            raise PublishError(f"{self.label} {action} failed: {exc}") from exc

    def _translate(self, action: str, exc: Any) -> S3Error:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        where = f"bucket {self.bucket!r} on {self.endpoint_description()}"
        if code in ("NoSuchBucket", "404"):
            message = f"{where} was not found. Check the bucket name and --endpoint-url."
        elif code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken"):
            message = (
                f"the credentials for {where} were rejected ({code}). Check the profile or "
                "environment variables that boto3 reads."
            )
        elif code in ("AccessDenied", "403", "AllAccessDisabled", "Forbidden"):
            message = f"access denied for {where}: the credentials need {_NEEDS[action]}."
            if self.is_r2 and action in ("read-cors", "write-cors"):
                message += (
                    " R2 object read and write tokens cannot manage CORS; use a token with "
                    "bucket admin permission, or run `npx wrangler r2 bucket cors set BUCKET "
                    "--file deploy/r2-cors.json`."
                )
        else:
            message = f"{action} on {where} failed with {code or 'an error'}: {exc}"
        return S3Error(message, code)
