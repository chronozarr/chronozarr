"""`StorageAdapter` for Google Cloud Storage on google-cloud-storage.

Destination: `gs://BUCKET/PREFIX`. Credentials come only from Application Default Credentials
(`gcloud auth application-default login`, GOOGLE_APPLICATION_CREDENTIALS, or the service account
attached to the machine). This module never reads, prints or stores them, and it never changes
public access, IAM or the bucket's access mode.

Two GCS facts shape the adapter. Objects are readable by browsers at
`https://storage.googleapis.com/BUCKET/PREFIX` only when the bucket (or a CDN in front of it)
allows anonymous reads. The `storage.cloud.google.com` host authenticates with cookies and does
not answer CORS, so it is never offered as a store URL. And GCS reports an object's MD5 as
base64; the adapter converts it to the hex digest that `publish` compares.
"""

from __future__ import annotations

import base64
import copy
import urllib.parse
from pathlib import Path
from typing import Any, NoReturn

from chronozarr.publish import PublishError, RemoteObject

PUBLIC_HOST = "https://storage.googleapis.com"
UPLOAD_TIMEOUT_S = 300

# GCS has one `responseHeader` list, used for both Access-Control-Allow-Headers and
# Access-Control-Expose-Headers (docs/hosting.md section 3.3).
VIEWER_CORS_RULE: dict[str, Any] = {
    "origin": ["*"],
    "method": ["GET", "HEAD"],
    "responseHeader": [
        "Content-Type",
        "Range",
        "Content-Range",
        "Content-Length",
        "ETag",
        "Accept-Ranges",
    ],
    "maxAgeSeconds": 86400,
}

_NEEDS = {
    "list": "storage.objects.list",
    "put": "storage.objects.create (and storage.objects.delete to overwrite)",
    "read-cors": "storage.buckets.get",
    "write-cors": "storage.buckets.update",
}


class GcsAdapter:
    """One bucket on Google Cloud Storage."""

    label = "Google Cloud Storage"

    def __init__(self, bucket: str):
        try:
            from google.auth.exceptions import DefaultCredentialsError
            from google.cloud import storage
        except ImportError as exc:
            raise PublishError(
                "publishing to gs:// needs google-cloud-storage: run "
                "`uv sync --extra publish-gcs` (or `pip install 'chronozarr[publish-gcs]'`)."
            ) from exc
        self.bucket_name = bucket
        try:
            # project=None skips project inference: bucket operations do not need a project.
            self.client = storage.Client(project=None)
        except DefaultCredentialsError as exc:
            raise PublishError(
                "no Google Cloud credentials found. Run `gcloud auth application-default login`, "
                "set GOOGLE_APPLICATION_CREDENTIALS to a service-account key file, or run on "
                "Google Cloud with a service account attached."
            ) from exc
        self.bucket = self.client.bucket(bucket)

    def endpoint_description(self) -> str:
        return (
            f"{self.label} bucket {self.bucket_name!r} (JSON API, authenticated with "
            "Application Default Credentials; not a browser address)"
        )

    def default_public_url(self, prefix: str) -> str | None:
        """`https://storage.googleapis.com/BUCKET/PREFIX`; public only if the bucket is."""
        path = urllib.parse.quote(prefix, safe="/")
        return f"{PUBLIC_HOST}/{self.bucket_name}" + (f"/{path}" if path else "")

    def list_objects(self, prefix: str) -> dict[str, RemoteObject]:
        found: dict[str, RemoteObject] = {}
        listing = self.client.list_blobs(self.bucket_name, prefix=f"{prefix}/" if prefix else None)
        for blob in self._iterate("list", listing):
            found[blob.name] = RemoteObject(int(blob.size or 0), _hex_md5(blob.md5_hash))
        return found

    def put_object(self, key: str, path: Path, *, cache_control: str, content_type: str) -> None:
        from google.cloud.storage.retry import DEFAULT_RETRY

        blob = self.bucket.blob(key)
        blob.cache_control = cache_control
        # Replacing the same bytes is safe to repeat, so retry transient failures explicitly:
        # older client versions only retry uploads that carry a generation precondition.
        self._call(
            "put",
            blob.upload_from_filename,
            str(path),
            content_type=content_type,
            checksum="md5",
            retry=DEFAULT_RETRY,
            timeout=UPLOAD_TIMEOUT_S,
        )

    def read_cors(self) -> list[dict[str, Any]]:
        self._call("read-cors", self.bucket.reload, fields="cors")
        return [dict(rule) for rule in self.bucket.cors]

    def write_cors(self, rules: list[dict[str, Any]]) -> None:
        self.bucket.cors = rules
        self._call("write-cors", self.bucket.patch)

    def cors_rule(self) -> dict[str, Any]:
        return copy.deepcopy(VIEWER_CORS_RULE)

    def access_help(self) -> str:
        return (
            "The objects are stored but browsers cannot read them from the public URL. "
            "chronozarr never changes public access or IAM. To serve the bucket directly, grant "
            "`allUsers` the role roles/storage.objectViewer on it; a bucket with public access "
            "prevention enforced (bucket setting or organization policy) rejects that, and then "
            "the store needs a Cloud CDN backend bucket behind an external HTTPS load balancer, "
            "whose URL you pass as --public-url. Use storage.googleapis.com URLs: "
            "storage.cloud.google.com authenticates with cookies and does not answer CORS. "
            "docs/hosting.md section 3.3 has the commands."
        )

    def _iterate(self, action: str, items: Any) -> Any:
        """Iterate a client listing, turning provider errors into `PublishError`."""
        try:
            yield from items
        except Exception as exc:
            self._reraise(action, exc)

    def _call(self, action: str, method: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            self._reraise(action, exc)

    def _reraise(self, action: str, exc: Exception) -> NoReturn:
        """Raise `PublishError` for provider errors; any other exception propagates as is."""
        from google.api_core import exceptions as api
        from google.auth import exceptions as auth

        where = f"bucket {self.bucket_name!r} on {self.label}"
        if isinstance(exc, api.NotFound):
            message = f"{where} was not found. Check the bucket name in --destination."
        elif isinstance(exc, (api.Unauthorized, auth.GoogleAuthError)):
            message = (
                f"the credentials for {where} were rejected or have expired. Run `gcloud auth "
                "application-default login` again, or check GOOGLE_APPLICATION_CREDENTIALS."
            )
        elif isinstance(exc, api.Forbidden):
            message = f"access denied for {where}: the credentials need {_NEEDS[action]}."
        elif isinstance(exc, api.GoogleAPIError):
            message = f"{action} on {where} failed: {type(exc).__name__}: {exc}"
        else:
            raise exc
        raise PublishError(message) from exc


def _hex_md5(md5_hash: str | None) -> str:
    """GCS reports MD5 as base64; composite objects report none."""
    if not md5_hash:
        return ""
    return base64.b64decode(md5_hash).hex()
