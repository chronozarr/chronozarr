"""Choose the `StorageAdapter` for a destination scheme.

Adding a provider means a new `_publish_<name>.py` module with a class that satisfies
`chronozarr.publish.StorageAdapter`, and one branch below. Providers are imported on use so a
missing optional dependency only affects its own scheme.
"""

from __future__ import annotations

from chronozarr.publish import Destination, PublishError, StorageAdapter


def open_adapter(
    destination: Destination,
    *,
    profile: str | None = None,
    endpoint_url: str | None = None,
    region: str | None = None,
) -> StorageAdapter:
    if destination.scheme == "s3":
        from chronozarr._publish_s3 import S3Adapter

        return S3Adapter(
            destination.bucket, profile=profile, endpoint_url=endpoint_url, region=region
        )
    raise PublishError(
        f"unsupported destination scheme {destination.scheme}://. Supported: s3:// (AWS S3, and "
        "Cloudflare R2 or another S3-compatible service with --endpoint-url)."
    )
