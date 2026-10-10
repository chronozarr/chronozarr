"""Choose the `StorageAdapter` for a destination scheme.

Adding a provider means a new `_publish_<name>.py` module with a class that satisfies
`chronozarr.publish.StorageAdapter`, and one branch below. Providers are imported on use so a
missing optional dependency only affects its own scheme.
"""

from __future__ import annotations

from chronozarr.publish import Destination, PublishError, StorageAdapter


def _reject_s3_options(
    scheme: str, *, profile: str | None, endpoint_url: str | None, region: str | None
) -> None:
    options = (("--profile", profile), ("--endpoint-url", endpoint_url), ("--region", region))
    for flag, value in options:
        if value is not None:
            raise PublishError(
                f"{flag} is an S3 option and does not apply to {scheme}:// destinations. "
                "Credentials come from the provider's own chain (docs/hosting.md)."
            )


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
    if destination.scheme == "gs":
        from chronozarr._publish_gcs import GcsAdapter

        _reject_s3_options("gs", profile=profile, endpoint_url=endpoint_url, region=region)
        return GcsAdapter(destination.bucket)
    if destination.scheme == "az":
        from chronozarr._publish_azure import AzureAdapter

        _reject_s3_options("az", profile=profile, endpoint_url=endpoint_url, region=region)
        return AzureAdapter(destination.bucket, destination.prefix)
    raise PublishError(
        f"unsupported destination scheme {destination.scheme}://. Supported: s3:// (AWS S3, and "
        "Cloudflare R2 or another S3-compatible service with --endpoint-url), gs:// (Google "
        "Cloud Storage), and az:// (Azure Blob Storage)."
    )
