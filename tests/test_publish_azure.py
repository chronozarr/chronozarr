"""The Azure Blob Storage adapter against a fake `BlobServiceClient`: no request is sent.

The fake stands where azure-storage-blob's service and container clients stand. Provider
exceptions, `ContentSettings` and `CorsRule` are the SDK's own, so error translation and the CORS
conversion are tested on the types the SDK uses.
"""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace
from typing import Any

import azure.identity
import azure.storage.blob as blob_module
import pytest
from azure.core import exceptions as azure_errors
from azure.storage.blob import CorsRule

import chronozarr._publish_adapters as adapters_module
from chronozarr._publish_azure import MAX_CORS_RULES, VIEWER_CORS_RULE, AzureAdapter
from chronozarr.doctor import Check
from chronozarr.publish import (
    Destination,
    PublishError,
    parse_destination,
    plan_store,
    publish,
    upload,
)
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit

ACCOUNT_URL = "https://myaccount.blob.core.windows.net"
PUBLIC = "https://data.example.com/aoi/store-v1"
DEST = Destination("az", "myaccount", "stores/aoi/store-v1")
SECRET = "SuperSecretAccountKey=="


class FakeContainer:
    def __init__(self, service: FakeService, name: str):
        self.service = service
        self.name = name

    def list_blobs(self, name_starts_with: str | None = None):
        self.service.listings.append((self.name, name_starts_with))
        if self.service.list_error:
            raise self.service.list_error
        for name, (size, md5) in self.service.objects.items():
            if name.startswith(name_starts_with or ""):
                yield SimpleNamespace(
                    name=name, size=size, content_settings=SimpleNamespace(content_md5=md5)
                )

    def upload_blob(self, name: str, data: Any, **options: Any) -> None:
        if name in self.service.fail_names:
            raise self.service.fail_names[name]
        content = data.read()
        settings = options["content_settings"]
        self.service.uploads.append(
            {
                "name": name,
                "bytes": len(content),
                "overwrite": options.get("overwrite"),
                "cache_control": settings.cache_control,
                "content_type": settings.content_type,
                "content_md5": bytes(settings.content_md5),
            }
        )
        self.service.objects[name] = (len(content), bytearray(hashlib.md5(content).digest()))


class FakeService:
    """Records calls. `objects` maps blob name -> (size, Content-MD5 bytearray or None)."""

    def __init__(self, url: str = ACCOUNT_URL, account_name: str = "myaccount"):
        self.url = url
        self.account_name = account_name
        self.constructed_with: dict[str, Any] = {}
        self.objects: dict[str, tuple[int, bytearray | None]] = {}
        self.uploads: list[dict[str, Any]] = []
        self.listings: list[tuple[str, str | None]] = []
        self.fail_names: dict[str, Exception] = {}
        self.list_error: Exception | None = None
        self.cors: list[CorsRule] = []
        self.cors_read_error: Exception | None = None
        self.cors_write_error: Exception | None = None
        self.cors_writes: list[list[CorsRule]] = []
        self.properties_calls = 0

    def get_container_client(self, name: str) -> FakeContainer:
        return FakeContainer(self, name)

    def get_service_properties(self) -> dict[str, Any]:
        if self.cors_read_error:
            raise self.cors_read_error
        return {"cors": list(self.cors)}

    def set_service_properties(self, **options: Any) -> None:
        if self.cors_write_error:
            raise self.cors_write_error
        assert set(options) == {"cors"}, "only CORS may be changed"
        self.cors_writes.append(options["cors"])
        self.cors = options["cors"]


class FakeCredential:
    pass


@pytest.fixture(autouse=True)
def no_connection_string(monkeypatch):
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)


@pytest.fixture
def service(monkeypatch) -> FakeService:
    fake = FakeService()

    class FakeClientClass:
        def __new__(cls, **options: Any):
            fake.constructed_with = options
            return fake

        @classmethod
        def from_connection_string(cls, conn_str: str, **options: Any):
            fake.constructed_with = {"connection_string": conn_str, **options}
            return fake

    monkeypatch.setattr(blob_module, "BlobServiceClient", FakeClientClass)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", FakeCredential)
    return fake


@pytest.fixture
def adapter(service) -> AzureAdapter:
    return AzureAdapter("myaccount", "stores/aoi/store-v1")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("publish_azure") / "store"
    build_store(path, make_truth(2, 2, 40, 50), shard=False, chunk_size=16)
    return path


def plan_for(store, adapter):
    return plan_store(store, DEST, adapter, PUBLIC)


def passing(url: str) -> list[Check]:
    return [Check("root zarr.json", "ok", "200")]


def md5_of(data: bytes) -> bytearray:
    return bytearray(hashlib.md5(data).digest())


# -- destination syntax ------------------------------------------------------------------------


def test_open_adapter_dispatches_az_to_the_azure_adapter(service):
    adapter = adapters_module.open_adapter(DEST)
    assert isinstance(adapter, AzureAdapter)
    assert adapter.label == "Azure Blob Storage"
    assert (adapter.account, adapter.container_name) == ("myaccount", "stores")


def test_the_destination_is_account_container_prefix():
    dest = parse_destination("az://myaccount/stores/aoi/store-v1")
    assert (dest.scheme, dest.bucket, dest.prefix) == ("az", "myaccount", "stores/aoi/store-v1")
    assert dest.uri == "az://myaccount/stores/aoi/store-v1"


def test_the_https_blob_address_is_a_public_url_not_a_destination():
    with pytest.raises(PublishError, match=r"web URL.*--public-url"):
        parse_destination("https://myaccount.blob.core.windows.net/stores/aoi")


@pytest.mark.parametrize(
    "destination",
    [
        Destination("az", "myaccount", ""),  # no container
        Destination("az", "MyAccount", "stores/p"),
        Destination("az", "ab", "stores/p"),
        Destination("az", "evil.example.com", "stores/p"),  # a host, not an account name
        Destination("az", "myaccount", "Bad_Container/p"),
    ],
)
def test_a_destination_that_is_not_account_container_is_refused_before_any_client(
    service, destination
):
    with pytest.raises(PublishError, match=r"az://ACCOUNT/CONTAINER/PREFIX"):
        adapters_module.open_adapter(destination)
    assert service.constructed_with == {}


@pytest.mark.parametrize(
    ("flag", "options"),
    [
        ("--profile", {"profile": "x"}),
        ("--endpoint-url", {"endpoint_url": "https://x.test"}),
        ("--region", {"region": "us-east-1"}),
    ],
)
def test_s3_only_options_are_refused_for_az(service, flag, options):
    with pytest.raises(PublishError, match=f"{flag} is an S3 option"):
        adapters_module.open_adapter(DEST, **options)


# -- construction and credentials --------------------------------------------------------------


def test_default_azure_credential_and_retries_without_a_connection_string(service, adapter):
    options = service.constructed_with
    assert options["account_url"] == ACCOUNT_URL
    assert isinstance(options["credential"], FakeCredential)
    assert options["retry_total"] == 5
    assert "DefaultAzureCredential" in adapter.endpoint_description()


def test_a_connection_string_from_the_environment_is_used_and_never_printed(service, monkeypatch):
    monkeypatch.setenv(
        "AZURE_STORAGE_CONNECTION_STRING", f"AccountName=myaccount;AccountKey={SECRET}"
    )
    adapter = AzureAdapter("myaccount", "stores/p")
    assert service.constructed_with["connection_string"].endswith(SECRET)
    assert service.constructed_with["retry_total"] == 5
    text = adapter.endpoint_description() + adapter.access_help()
    assert "AZURE_STORAGE_CONNECTION_STRING" in text
    assert SECRET not in text


def test_a_connection_string_for_another_account_is_refused(service, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", f"AccountName=other;AccountKey={SECRET}")
    service.account_name = "other"
    with pytest.raises(PublishError, match=r"'other'.*names 'myaccount'") as raised:
        AzureAdapter("myaccount", "stores/p")
    assert SECRET not in str(raised.value)


def test_an_invalid_connection_string_does_not_echo_it(service, monkeypatch):
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", f"garbage {SECRET}")

    class Broken:
        @classmethod
        def from_connection_string(cls, conn_str: str, **options: Any):
            raise ValueError(f"Connection string missing required connection details: {conn_str}")

    monkeypatch.setattr(blob_module, "BlobServiceClient", Broken)
    with pytest.raises(
        PublishError, match="not a valid Azure storage connection string"
    ) as raised:
        AzureAdapter("myaccount", "stores/p")
    assert SECRET not in str(raised.value)


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "azure.identity", None)
    with pytest.raises(PublishError, match=r"uv sync --extra publish-azure"):
        AzureAdapter("myaccount", "stores/p")


def test_no_credentials_is_an_actionable_error_on_first_use(service, adapter):
    service.list_error = azure_errors.ClientAuthenticationError(
        f"DefaultAzureCredential failed to retrieve a token: {SECRET}"
    )
    with pytest.raises(PublishError, match=r"az login.*AZURE_CLIENT_ID") as raised:
        adapter.list_objects("stores/p")
    assert SECRET not in str(raised.value)


# -- public URL --------------------------------------------------------------------------------


def test_public_url_is_the_blob_endpoint_with_container_and_encoded_prefix(adapter):
    assert (
        adapter.default_public_url("stores/aoi/store v1")
        == "https://myaccount.blob.core.windows.net/stores/aoi/store%20v1"
    )


def test_public_url_for_the_container_root_has_no_trailing_slash(adapter):
    assert adapter.default_public_url("stores") == f"{ACCOUNT_URL}/stores"


def test_the_plan_derives_the_public_url_from_the_destination(store, adapter):
    plan = plan_store(store, DEST, adapter, None)
    assert plan.public_url == f"{ACCOUNT_URL}/stores/aoi/store-v1"
    assert plan.public_url_derived
    assert plan_for(store, adapter).public_url == PUBLIC


def test_a_shared_access_signature_in_the_endpoint_never_reaches_the_url_or_messages(
    service, adapter
):
    service.url = f"{ACCOUNT_URL}/?sv=2024&sig={SECRET}"
    assert SECRET not in adapter.default_public_url("stores/p")
    service.list_error = azure_errors.ServiceRequestError("dns")
    with pytest.raises(PublishError, match="could not reach") as raised:
        adapter.list_objects("stores/p")
    assert SECRET not in str(raised.value)


# -- listing -----------------------------------------------------------------------------------


def test_list_objects_reports_hex_md5_and_keys_with_the_container(service, adapter):
    service.objects = {
        "aoi/store-v1/zarr.json": (7, md5_of(b"{}....")),
        "aoi/store-v1/0/c": (3, None),
    }
    found = adapter.list_objects("stores/aoi/store-v1")
    assert service.listings == [("stores", "aoi/store-v1/")]
    assert found["stores/aoi/store-v1/zarr.json"].size == 7
    assert found["stores/aoi/store-v1/zarr.json"].etag == hashlib.md5(b"{}....").hexdigest()
    assert found["stores/aoi/store-v1/0/c"].etag == ""  # no Content-MD5 stored


def test_listing_the_container_root_passes_no_prefix(service, adapter):
    service.objects = {"zarr.json": (2, None)}
    assert set(adapter.list_objects("stores")) == {"stores/zarr.json"}
    assert service.listings == [("stores", None)]


def test_a_prefix_outside_the_container_is_refused(service, adapter):
    with pytest.raises(PublishError, match="outside container 'stores'"):
        adapter.list_objects("other/p")


# -- errors ------------------------------------------------------------------------------------


def test_a_missing_container_is_reported_and_not_created(service, adapter):
    service.list_error = azure_errors.ResourceNotFoundError("ContainerNotFound")
    with pytest.raises(
        PublishError, match=r"container 'stores' in account 'myaccount' was not found"
    ):
        adapter.list_objects("stores/p")


def test_an_unreachable_account_names_the_endpoint(service, adapter):
    service.list_error = azure_errors.ServiceRequestError("Name or service not known")
    with pytest.raises(PublishError, match=r"could not reach https://myaccount\.blob\.core"):
        adapter.list_objects("stores/p")


class StorageError(azure_errors.HttpResponseError):
    """What azure-storage-blob raises: an HttpResponseError that carries `error_code`."""

    error_code: str | None = None


def forbidden(code: str = "AuthorizationPermissionMismatch") -> StorageError:
    error = StorageError("This request is not authorized for user@example.org")
    error.status_code = 403
    error.error_code = code
    return error


def test_missing_data_role_is_named_for_listing_and_uploading(service, adapter, tmp_path):
    service.list_error = forbidden()
    with pytest.raises(
        PublishError, match=r"AuthorizationPermissionMismatch.*Storage Blob Data"
    ) as raised:
        adapter.list_objects("stores/p")
    assert "user@example.org" not in str(raised.value)
    path = tmp_path / "chunk"
    path.write_bytes(b"x")
    service.fail_names["p/c"] = forbidden()
    with pytest.raises(PublishError, match="Storage Blob Data Contributor"):
        adapter.put_object("stores/p/c", path, cache_control="x", content_type="y")


def test_other_azure_errors_keep_their_type_and_message(service, adapter):
    service.list_error = azure_errors.ServiceResponseError("connection reset")
    with pytest.raises(PublishError, match=r"ServiceResponseError.*connection reset"):
        adapter.list_objects("stores/p")


def test_errors_that_are_not_the_providers_propagate_unchanged(service, adapter):
    service.list_error = RuntimeError("a bug")
    with pytest.raises(RuntimeError, match="a bug"):
        adapter.list_objects("stores/p")


# -- storing -----------------------------------------------------------------------------------


def test_put_strips_the_container_and_sets_headers_md5_and_overwrite(service, adapter, tmp_path):
    path = tmp_path / "chunk"
    path.write_bytes(b"0123456789")
    adapter.put_object(
        "stores/aoi/store-v1/0/c",
        path,
        cache_control="public, max-age=300",
        content_type="application/json",
    )
    (sent,) = service.uploads
    assert sent["name"] == "aoi/store-v1/0/c"
    assert sent["cache_control"] == "public, max-age=300"
    assert sent["content_type"] == "application/json"
    assert sent["content_md5"] == hashlib.md5(b"0123456789").digest()
    assert sent["overwrite"] is True


def test_publish_flow_uploads_chunks_then_metadata_then_root(store, service, adapter):
    plan = plan_for(store, adapter)
    report = upload(plan, adapter, workers=1)
    names = [u["name"] for u in service.uploads]
    assert report.uploaded == len(plan.objects)
    assert names == [f"aoi/store-v1/{o.key}" for o in plan.objects]
    assert names[-1] == "aoi/store-v1/zarr.json"
    by_name = {u["name"]: u for u in service.uploads}
    for obj in plan.objects:
        sent = by_name[f"aoi/store-v1/{obj.key}"]
        assert sent["cache_control"] == obj.cache_control
        assert sent["content_type"] == obj.content_type


def test_a_rerun_skips_matching_md5_and_refuses_a_changed_object(store, service, adapter):
    plan = plan_for(store, adapter)
    upload(plan, adapter, workers=1)
    stored = len(service.uploads)
    assert upload(plan, adapter, workers=1).uploaded == 0
    assert len(service.uploads) == stored

    chunk = next(o for o in plan.objects if o.phase == 0)
    name = f"aoi/store-v1/{chunk.key}"
    service.objects[name] = (service.objects[name][0], md5_of(b"same size?"))
    with pytest.raises(PublishError, match="Refusing to overwrite"):
        upload(plan, adapter, workers=1)
    assert upload(plan, adapter, overwrite=True, workers=1).uploaded == 1


def test_an_interrupted_upload_resumes_without_a_root_in_the_container(store, service, adapter):
    plan = plan_for(store, adapter)
    victim = [o for o in plan.objects if o.phase == 0][-1]
    name = f"aoi/store-v1/{victim.key}"
    service.fail_names[name] = azure_errors.ServiceResponseError("flaky")
    with pytest.raises(PublishError, match=r"phase 1 \(chunks\).*ServiceResponseError"):
        upload(plan, adapter, workers=1)
    assert "aoi/store-v1/zarr.json" not in service.objects
    del service.fail_names[name]
    assert upload(plan, adapter, workers=1).skipped > 0
    assert "aoi/store-v1/zarr.json" in service.objects


# -- CORS --------------------------------------------------------------------------------------


def app_rule() -> CorsRule:
    return CorsRule(
        ["https://app.example.org", "https://other.example.org"],
        ["PUT", "GET"],
        allowed_headers=["x-ms-meta-*"],
        exposed_headers=["ETag"],
        max_age_in_seconds=60,
    )


APP_DICT = {
    "allowed_origins": ["https://app.example.org", "https://other.example.org"],
    "allowed_methods": ["PUT", "GET"],
    "allowed_headers": ["x-ms-meta-*"],
    "exposed_headers": ["ETag"],
    "max_age_in_seconds": 60,
}


def cors_failing_then_passing():
    calls: list[int] = []

    def doctor(url: str) -> list[Check]:
        calls.append(1)
        if len(calls) == 1:
            return [Check("CORS on zarr.json", "fail", "no header", "set CORS")]
        return [Check("root zarr.json", "ok", "200")]

    return doctor, calls


def test_read_cors_splits_the_sdks_comma_joined_strings(service, adapter):
    service.cors = [app_rule()]
    assert adapter.read_cors() == [APP_DICT]
    service.cors = []
    assert adapter.read_cors() == []


def test_write_cors_converts_every_rule_and_changes_nothing_else(service, adapter):
    adapter.write_cors([APP_DICT, VIEWER_CORS_RULE])
    (written,) = service.cors_writes
    assert [r.allowed_origins for r in written] == [
        "https://app.example.org,https://other.example.org",
        "*",
    ]
    assert written[0].allowed_methods == "PUT,GET"
    assert written[0].max_age_in_seconds == 60
    assert written[1].allowed_headers == "Range,If-Match,If-None-Match,Content-Type"
    assert written[1].exposed_headers == "Content-Range,Content-Length,ETag,Accept-Ranges"
    assert adapter.read_cors() == [APP_DICT, VIEWER_CORS_RULE]


def test_more_than_five_rules_is_refused_and_says_the_setting_is_account_wide(service, adapter):
    with pytest.raises(PublishError, match=r"6 rules and Azure allows 5.*every container"):
        adapter.write_cors([APP_DICT] * (MAX_CORS_RULES + 1))
    assert service.cors_writes == []


def test_cors_permission_errors_name_the_role_and_the_key_alternative(service, adapter):
    service.cors_read_error = forbidden()
    with pytest.raises(PublishError, match=r"blobServices/read.*AZURE_STORAGE_CONNECTION_STRING"):
        adapter.read_cors()
    service.cors_write_error = forbidden()
    with pytest.raises(PublishError, match=r"blobServices/write"):
        adapter.write_cors([VIEWER_CORS_RULE])


def test_the_viewer_rule_is_what_doctor_needs_and_is_a_copy(adapter):
    rule = adapter.cors_rule()
    assert rule["allowed_origins"] == ["*"]
    assert {"GET", "HEAD"} <= set(rule["allowed_methods"])
    assert "Range" in rule["allowed_headers"]
    assert "Content-Range" in rule["exposed_headers"]
    rule["allowed_origins"].append("mutated")
    assert VIEWER_CORS_RULE["allowed_origins"] == ["*"]


def test_cors_is_only_read_without_apply_cors(store, service, adapter):
    service.cors = [app_rule()]
    doctor, _ = cors_failing_then_passing()
    result = publish(plan_for(store, adapter), adapter, workers=1, check_store=doctor)
    assert service.cors_writes == []
    assert not result.cors_changed
    assert result.link is None
    assert "--apply-cors" in result.notes[0]
    assert "1 CORS rule(s)" in result.notes[0]


def test_apply_cors_keeps_existing_rules_and_appends_the_viewer_rule_last(store, service, adapter):
    service.cors = [app_rule()]
    doctor, calls = cors_failing_then_passing()
    result = publish(
        plan_for(store, adapter), adapter, apply_cors=True, workers=1, check_store=doctor
    )
    (written,) = service.cors_writes
    assert written[0].allowed_origins.startswith("https://app.example.org")
    assert written[-1].allowed_origins == "*"
    assert adapter.read_cors() == [APP_DICT, VIEWER_CORS_RULE]
    assert result.cors_changed
    assert len(calls) == 2
    assert result.link is not None


def test_cors_is_not_written_again_when_the_viewer_rule_is_already_there(store, service, adapter):
    service.cors = [CorsRule(**VIEWER_CORS_RULE)]
    doctor, _ = cors_failing_then_passing()
    result = publish(
        plan_for(store, adapter), adapter, apply_cors=True, workers=1, check_store=doctor
    )
    assert service.cors_writes == []
    assert not result.cors_changed
    assert "already has the viewer rule" in result.notes[0]


def test_cors_is_not_touched_when_the_checks_pass(store, service, adapter):
    service.cors = [app_rule()]
    result = publish(
        plan_for(store, adapter), adapter, apply_cors=True, workers=1, check_store=passing
    )
    assert service.cors_writes == []
    assert result.link is not None


def test_an_unreadable_cors_configuration_is_reported_and_nothing_is_written(
    store, service, adapter
):
    service.cors_read_error = forbidden()
    doctor, _ = cors_failing_then_passing()
    result = publish(
        plan_for(store, adapter), adapter, apply_cors=True, workers=1, check_store=doctor
    )
    assert service.cors_writes == []
    assert "blobServices/read" in result.notes[0]
    assert result.link is None


def test_access_help_explains_account_scope_and_the_two_anonymous_access_settings(adapter):
    text = adapter.access_help()
    assert "whole storage account" in text
    assert "allow-blob-public-access" in text
    assert "--public-access blob" in text
    assert "--public-url" in text
