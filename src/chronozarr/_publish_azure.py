"""`StorageAdapter` for Azure Blob Storage on azure-storage-blob.

Destination: `az://ACCOUNT/CONTAINER/PREFIX`, for example `az://myaccount/stores/aoi/store-v1`.
The account is part of the destination because a container name alone does not say which storage
account to write to. An `https://ACCOUNT.blob.core.windows.net/...` address is the browser URL,
not a destination: pass it as --public-url.

`publish` knows `Destination(scheme, bucket, prefix)`. Here `bucket` is the account and `prefix`
is `CONTAINER[/PREFIX]`, so every key `publish` hands over starts with the container name. The
adapter strips it for the SDK and puts it back on listed names.

Credentials: when AZURE_STORAGE_CONNECTION_STRING is set it is used, and the account in the
destination must be the one it names. Otherwise `DefaultAzureCredential` (environment variables,
workload and managed identity, Azure CLI, Azure PowerShell, developer sign-in). This module never
reads, prints or stores a secret.

CORS is a property of the whole Blob service of the storage account, not of a container. A change
affects every container in the account, so the adapter keeps every existing rule, appends the
viewer rule last, and refuses a write that would pass Azure's limit of five rules.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, NoReturn

from chronozarr.publish import PublishError, RemoteObject

CONNECTION_STRING_ENV = "AZURE_STORAGE_CONNECTION_STRING"
MAX_CORS_RULES = 5
RETRY_TOTAL = 5

_ACCOUNT = re.compile(r"^[a-z0-9]{3,24}$")
_CONTAINER = re.compile(r"^\$?[a-z0-9][a-z0-9-]{1,62}$")

# The viewer's requirements in Azure's CORS fields (lists here; the SDK joins them with commas).
VIEWER_CORS_RULE: dict[str, Any] = {
    "allowed_origins": ["*"],
    "allowed_methods": ["GET", "HEAD"],
    "allowed_headers": ["Range", "If-Match", "If-None-Match", "Content-Type"],
    "exposed_headers": ["Content-Range", "Content-Length", "ETag", "Accept-Ranges"],
    "max_age_in_seconds": 86400,
}

_ROLES = {
    "list": "the Storage Blob Data Reader or Storage Blob Data Contributor role",
    "put": "the Storage Blob Data Contributor role",
    "read-cors": "the Storage Account Contributor role (Microsoft.Storage/storageAccounts/"
    "blobServices/read), or an account key through AZURE_STORAGE_CONNECTION_STRING",
    "write-cors": "the Storage Account Contributor role (Microsoft.Storage/storageAccounts/"
    "blobServices/write), or an account key through AZURE_STORAGE_CONNECTION_STRING",
}


class AzureAdapter:
    """One container of one storage account, through the account's Blob service."""

    label = "Azure Blob Storage"

    def __init__(self, account: str, prefix: str):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
        except ImportError as exc:
            raise PublishError(
                "publishing to az:// needs azure-storage-blob and azure-identity: run "
                "`uv sync --extra publish-azure` (or `pip install 'chronozarr[publish-azure]'`)."
            ) from exc
        container, _, _ = prefix.partition("/")
        if not _ACCOUNT.match(account) or not _CONTAINER.match(container):
            raise PublishError(
                f"az://{account}/{prefix} is not an Azure destination. Expected "
                "az://ACCOUNT/CONTAINER/PREFIX: ACCOUNT is the storage account name (3 to 24 "
                "lowercase letters and digits), CONTAINER the container name, for example "
                "az://myaccount/stores/aoi/store-v1. The https://ACCOUNT.blob.core.windows.net "
                "address is the --public-url."
            )
        self.account = account
        self.container_name = container
        connection_string = os.environ.get(CONNECTION_STRING_ENV)
        self.uses_connection_string = bool(connection_string)
        if connection_string:
            try:
                self.service = BlobServiceClient.from_connection_string(
                    connection_string, retry_total=RETRY_TOTAL
                )
            except ValueError as exc:
                raise PublishError(
                    f"{CONNECTION_STRING_ENV} is not a valid Azure storage connection string. "
                    "Unset it to use DefaultAzureCredential instead."
                ) from exc
            if self.service.account_name != account:
                raise PublishError(
                    f"{CONNECTION_STRING_ENV} is for storage account "
                    f"{self.service.account_name!r}, but the destination names {account!r}. "
                    "Unset it, or use the destination it belongs to."
                )
        else:
            self.service = BlobServiceClient(
                account_url=f"https://{account}.blob.core.windows.net",
                credential=DefaultAzureCredential(),
                retry_total=RETRY_TOTAL,
            )
        self.container = self.service.get_container_client(container)

    def endpoint_description(self) -> str:
        credential = (
            f"the connection string in {CONNECTION_STRING_ENV}"
            if self.uses_connection_string
            else "DefaultAzureCredential"
        )
        return (
            f"{self.label} account {self.account!r}, container {self.container_name!r} "
            f"(authenticated with {credential}; not a browser address)"
        )

    def default_public_url(self, prefix: str) -> str | None:
        """The blob endpoint of the account: `https://ACCOUNT.blob.core.windows.net/CONTAINER/...`.

        `prefix` starts with the container name. The URL serves objects only when the account
        allows anonymous access and the container's access level is Blob.
        """
        return f"{self._endpoint()}/{urllib.parse.quote(prefix, safe='/')}"

    def list_objects(self, prefix: str) -> dict[str, RemoteObject]:
        found: dict[str, RemoteObject] = {}
        inner = self._blob_prefix(prefix)
        listing = self.container.list_blobs(name_starts_with=f"{inner}/" if inner else None)
        for blob in self._iterate("list", listing):
            md5 = blob.content_settings.content_md5
            found[f"{self.container_name}/{blob.name}"] = RemoteObject(
                int(blob.size), bytes(md5).hex() if md5 else ""
            )
        return found

    def put_object(self, key: str, path: Path, *, cache_control: str, content_type: str) -> None:
        from azure.storage.blob import ContentSettings

        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        settings = ContentSettings(
            content_type=content_type,
            cache_control=cache_control,
            content_md5=bytearray(digest.digest()),
        )
        with path.open("rb") as body:
            self._call(
                "put",
                self.container.upload_blob,
                self._blob_prefix(key),
                body,
                overwrite=True,
                content_settings=settings,
            )

    def read_cors(self) -> list[dict[str, Any]]:
        properties = self._call("read-cors", self.service.get_service_properties)
        return [_rule_to_dict(rule) for rule in properties.get("cors") or []]

    def write_cors(self, rules: list[dict[str, Any]]) -> None:
        from azure.storage.blob import CorsRule

        if len(rules) > MAX_CORS_RULES:
            raise PublishError(
                f"the storage account's CORS configuration would have {len(rules)} rules and "
                f"Azure allows {MAX_CORS_RULES}. Merge or remove a rule yourself (the setting is "
                "shared by every container in the account), or serve the store through a CDN "
                "and pass its URL as --public-url."
            )
        sdk_rules = [CorsRule(**rule) for rule in rules]
        self._call("write-cors", self.service.set_service_properties, cors=sdk_rules)

    def cors_rule(self) -> dict[str, Any]:
        return copy.deepcopy(VIEWER_CORS_RULE)

    def access_help(self) -> str:
        return (
            "The objects are stored but browsers cannot read them from the public URL. "
            "chronozarr never changes access settings. Anonymous reads need two settings: the "
            "storage account must allow anonymous access to blobs (`az storage account update "
            "--allow-blob-public-access true`), and the container's public access level must be "
            "Blob (`az storage container set-permission --public-access blob`). An organization "
            "policy can forbid both; then put Azure Front Door or Azure CDN in front of the "
            "container and pass its URL as --public-url. CORS for Blob storage is set on the "
            "whole storage account, not the container: `--apply-cors` adds the viewer rule to "
            "every container's rules in the account and keeps the existing ones."
        )

    def _endpoint(self) -> str:
        """The account's blob endpoint without a query string: a connection string may carry a
        shared access signature, and that must not reach a message or a link."""
        parts = urllib.parse.urlsplit(self.service.url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")

    def _blob_prefix(self, key: str) -> str:
        """`key` ('CONTAINER[/NAME]') without the container."""
        container, _, inner = key.partition("/")
        if container != self.container_name:
            raise PublishError(f"{key!r} is outside container {self.container_name!r}.")
        return inner

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
        from azure.core import exceptions as azure

        where = f"container {self.container_name!r} in account {self.account!r}"
        # azure-storage-blob sets `error_code` (for example AuthorizationPermissionMismatch).
        error_code = getattr(exc, "error_code", None)
        if isinstance(exc, azure.ClientAuthenticationError):
            message = (
                f"Azure rejected the credentials for {where}, or none were found. Sign in with "
                "`az login`, or set the AZURE_CLIENT_ID, AZURE_TENANT_ID and AZURE_CLIENT_SECRET "
                f"of a service principal, or set {CONNECTION_STRING_ENV}."
            )
        elif isinstance(exc, azure.ResourceNotFoundError):
            message = (
                f"{where} was not found. Check ACCOUNT and CONTAINER in --destination; the "
                "command does not create containers."
            )
        elif isinstance(exc, azure.ServiceRequestError):
            message = (
                f"could not reach {self._endpoint()}: check the account name in --destination "
                "and the network connection."
            )
        elif isinstance(exc, azure.HttpResponseError) and exc.status_code == 403:
            code = f" ({error_code})" if error_code else ""
            message = (
                f"access denied{code} for {where}: the identity needs {_ROLES[action]}. A "
                "storage firewall or network rule can also cause this."
            )
        elif isinstance(exc, azure.AzureError):
            code = f" ({error_code})" if error_code else ""
            detail = f"{type(exc).__name__}{code}: {exc.message}"
            message = f"{action} on {where} failed: {detail}"
        else:
            raise exc
        raise PublishError(message) from exc


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _rule_to_dict(rule: Any) -> dict[str, Any]:
    return {
        "allowed_origins": _split(rule.allowed_origins),
        "allowed_methods": _split(rule.allowed_methods),
        "allowed_headers": _split(rule.allowed_headers),
        "exposed_headers": _split(rule.exposed_headers),
        "max_age_in_seconds": int(rule.max_age_in_seconds or 0),
    }
