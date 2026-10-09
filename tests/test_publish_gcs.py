"""The Google Cloud Storage adapter against a fake `storage.Client`: no request leaves the process.

The fake stands where google-cloud-storage's client, bucket and blob objects stand. Provider
exceptions are the real ones from google.api_core and google.auth, so error translation is tested
on the types the SDK raises.
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest
from google.api_core import exceptions as api
from google.auth import exceptions as auth
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY

import chronozarr._publish_adapters as adapters_module
from chronozarr._publish_gcs import VIEWER_CORS_RULE, GcsAdapter
from chronozarr.doctor import Check
from chronozarr.publish import (
    Destination,
    PublishError,
    plan_store,
    publish,
    upload,
)
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit

PUBLIC = "https://data.example.com/aoi/store-v1"


def b64_md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


class FakeBlob:
    def __init__(self, client: FakeClient, name: str):
        self.client = client
        self.name = name
        self.cache_control: str | None = None
        self.size = 0
        self.md5_hash: str | None = None

    def upload_from_filename(self, filename: str, **options: Any) -> None:
        if self.name in self.client.fail_keys:
            raise self.client.fail_keys[self.name]
        data = Path(filename).read_bytes()
        self.client.uploads.append(
            {
                "key": self.name,
                "cache_control": self.cache_control,
                "bytes": len(data),
                **options,
            }
        )
        self.client.objects[self.name] = (len(data), b64_md5(data))
        self.client.contents[self.name] = data

    def download_as_bytes(self, **options: Any) -> bytes:
        self.client.downloads.append({"key": self.name, **options})
        if self.client.read_error:
            raise self.client.read_error
        if self.name not in self.client.contents:
            raise api.NotFound(f"No such object: {self.name}")
        return self.client.contents[self.name]


class FakeBucket:
    def __init__(self, client: FakeClient, name: str):
        self.client = client
        self.name = name
        self.cors: list[dict[str, Any]] = list(client.initial_cors)
        self.patches: list[list[dict[str, Any]]] = []

    def blob(self, key: str) -> FakeBlob:
        return FakeBlob(self.client, key)

    def reload(self, **options: Any) -> None:
        if self.client.cors_read_error:
            raise self.client.cors_read_error
        self.cors = [dict(rule) for rule in self.client.initial_cors]

    def patch(self) -> None:
        if self.client.cors_write_error:
            raise self.client.cors_write_error
        self.patches.append(list(self.cors))


class FakeClient:
    """Records calls; `objects` maps key -> (size, base64 md5) like a bucket listing."""

    def __init__(self, **options: Any):
        self.constructed_with = options
        self.objects: dict[str, tuple[int, str | None]] = {}
        self.uploads: list[dict[str, Any]] = []
        self.listings: list[tuple[str, str | None]] = []
        self.fail_keys: dict[str, Exception] = {}
        self.contents: dict[str, bytes] = {}
        self.downloads: list[dict[str, Any]] = []
        self.read_error: Exception | None = None
        self.list_error: Exception | None = None
        self.initial_cors: list[dict[str, Any]] = []
        self.cors_read_error: Exception | None = None
        self.cors_write_error: Exception | None = None
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(self, name))

    def list_blobs(self, bucket: str, prefix: str | None = None):
        self.listings.append((bucket, prefix))
        if self.list_error:
            raise self.list_error
        for key, (size, md5) in self.objects.items():
            if key.startswith(prefix or ""):
                blob = FakeBlob(self, key)
                blob.size, blob.md5_hash = size, md5
                yield blob


@pytest.fixture
def client(monkeypatch) -> FakeClient:
    fake = FakeClient()

    def factory(**options: Any) -> FakeClient:
        fake.constructed_with = options
        return fake

    monkeypatch.setattr(storage, "Client", factory)
    return fake


@pytest.fixture
def adapter(client) -> GcsAdapter:
    return GcsAdapter("my-bucket")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("publish_gcs") / "store"
    build_store(path, make_truth(2, 2, 40, 50), shard=False, chunk_size=16)
    return path


def plan_for(store, adapter, prefix="aoi/store-v1"):
    return plan_store(store, Destination("gs", "my-bucket", prefix), adapter, PUBLIC)


def passing(url: str) -> list[Check]:
    return [Check("root zarr.json", "ok", "200")]


# -- construction ------------------------------------------------------------------------------


def test_open_adapter_dispatches_gs_to_the_gcs_adapter(client):
    adapter = adapters_module.open_adapter(Destination("gs", "my-bucket", "p"))
    assert isinstance(adapter, GcsAdapter)
    assert adapter.label == "Google Cloud Storage"


def test_the_client_is_built_without_project_inference(client, adapter):
    assert client.constructed_with == {"project": None}


@pytest.mark.parametrize(
    ("flag", "options"),
    [
        ("--profile", {"profile": "x"}),
        ("--endpoint-url", {"endpoint_url": "https://x.test"}),
        ("--region", {"region": "us-east-1"}),
    ],
)
def test_s3_only_options_are_refused_for_gs(client, flag, options):
    with pytest.raises(PublishError, match=f"{flag} is an S3 option"):
        adapters_module.open_adapter(Destination("gs", "my-bucket", "p"), **options)


def test_missing_credentials_say_how_to_get_application_default_credentials(monkeypatch):
    def no_credentials(**options: Any):
        raise auth.DefaultCredentialsError("could not find /home/someone/key.json")

    monkeypatch.setattr(storage, "Client", no_credentials)
    with pytest.raises(PublishError, match="gcloud auth application-default login") as raised:
        GcsAdapter("my-bucket")
    assert "someone" not in str(raised.value)


def test_missing_sdk_names_the_extra(monkeypatch):
    monkeypatch.delattr("google.cloud.storage")  # the package attribute would satisfy the import
    monkeypatch.setitem(sys.modules, "google.cloud.storage", None)
    with pytest.raises(PublishError, match=r"uv sync --extra publish-gcs"):
        GcsAdapter("my-bucket")


def test_the_description_names_the_bucket_and_says_it_is_not_a_browser_address(adapter):
    text = adapter.endpoint_description()
    assert "'my-bucket'" in text
    assert "not a browser address" in text


# -- public URL --------------------------------------------------------------------------------


def test_public_url_is_storage_googleapis_com_with_an_encoded_prefix(adapter):
    assert (
        adapter.default_public_url("aoi/store v1")
        == "https://storage.googleapis.com/my-bucket/aoi/store%20v1"
    )


def test_public_url_for_the_bucket_root_has_no_trailing_slash(adapter):
    assert adapter.default_public_url("") == "https://storage.googleapis.com/my-bucket"


def test_a_custom_public_url_wins_over_the_default(store, adapter):
    plan = plan_store(store, Destination("gs", "my-bucket", "p"), adapter, None)
    assert plan.public_url == "https://storage.googleapis.com/my-bucket/p"
    assert plan.public_url_derived
    custom = plan_for(store, adapter)
    assert custom.public_url == PUBLIC
    assert not custom.public_url_derived


def test_the_authenticated_console_host_is_never_produced(adapter):
    assert "storage.cloud.google.com" not in adapter.default_public_url("p")
    assert "storage.cloud.google.com" in adapter.access_help()  # named only to warn against it


# -- listing -----------------------------------------------------------------------------------


def test_list_objects_converts_base64_md5_to_hex_and_lists_under_the_prefix(client, adapter):
    client.objects = {
        "a/b/zarr.json": (7, b64_md5(b"{}....")),
        "a/b/0/c": (3, None),
    }
    found = adapter.list_objects("a/b")
    assert client.listings == [("my-bucket", "a/b/")]
    assert found["a/b/zarr.json"].size == 7
    assert found["a/b/zarr.json"].etag == hashlib.md5(b"{}....").hexdigest()
    assert found["a/b/0/c"].etag == ""  # composite objects report no MD5


def test_listing_the_bucket_root_passes_no_prefix(client, adapter):
    adapter.list_objects("")
    assert client.listings == [("my-bucket", None)]


def test_list_errors_say_which_permission_is_missing(client, adapter):
    client.list_error = api.Forbidden("Caller does not have access for user@example.org")
    with pytest.raises(PublishError, match=r"access denied.*storage\.objects\.list") as raised:
        adapter.list_objects("p")
    assert "user@example.org" not in str(raised.value)
    client.list_error = api.NotFound("no such bucket")
    with pytest.raises(PublishError, match="was not found"):
        adapter.list_objects("p")


@pytest.mark.parametrize("error", [api.Unauthorized("bad token abc123"), auth.RefreshError("x")])
def test_rejected_or_expired_credentials_are_a_clear_error(client, adapter, error):
    client.list_error = error
    with pytest.raises(PublishError, match="rejected or have expired") as raised:
        adapter.list_objects("p")
    assert "abc123" not in str(raised.value)


def test_other_provider_errors_keep_their_type_and_message(client, adapter):
    client.list_error = api.ServiceUnavailable("backend busy")
    with pytest.raises(PublishError, match=r"ServiceUnavailable.*backend busy"):
        adapter.list_objects("p")


def test_errors_that_are_not_the_providers_propagate_unchanged(client, adapter):
    client.list_error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        adapter.list_objects("p")
    client.list_error = RuntimeError("a bug")
    with pytest.raises(RuntimeError, match="a bug"):
        adapter.list_objects("p")


# -- storing -----------------------------------------------------------------------------------


def test_put_sets_cache_control_before_the_upload_with_md5_and_explicit_retry(
    client, adapter, tmp_path
):
    path = tmp_path / "chunk"
    path.write_bytes(b"0123456789")
    adapter.put_object(
        "p/0/c", path, cache_control="public, max-age=300", content_type="application/json"
    )
    (sent,) = client.uploads
    assert sent["key"] == "p/0/c"
    assert sent["cache_control"] == "public, max-age=300"
    assert sent["content_type"] == "application/json"
    assert sent["checksum"] == "md5"
    assert sent["retry"] is DEFAULT_RETRY
    assert sent["timeout"] > 60
    assert sent["bytes"] == 10


def test_put_errors_name_the_permission(client, adapter, tmp_path):
    path = tmp_path / "chunk"
    path.write_bytes(b"x")
    client.fail_keys["p/c"] = api.Forbidden("denied")
    with pytest.raises(PublishError, match=r"storage\.objects\.create"):
        adapter.put_object("p/c", path, cache_control="x", content_type="y")


def test_publish_flow_uploads_chunks_then_metadata_then_root(store, client, adapter):
    plan = plan_for(store, adapter)
    report = upload(plan, adapter, workers=1)
    keys = [u["key"] for u in client.uploads]
    assert report.uploaded == len(plan.objects)
    assert keys == [f"aoi/store-v1/{o.key}" for o in plan.objects]
    assert keys[-1] == "aoi/store-v1/zarr.json"
    by_key = {u["key"]: u for u in client.uploads}
    for obj in plan.objects:
        sent = by_key[f"aoi/store-v1/{obj.key}"]
        assert sent["cache_control"] == obj.cache_control
        assert sent["content_type"] == obj.content_type


def test_a_rerun_skips_objects_whose_md5_matches_and_stops_on_a_changed_one(
    store, client, adapter
):
    plan = plan_for(store, adapter)
    upload(plan, adapter, workers=1)
    first_uploads = len(client.uploads)
    assert upload(plan, adapter, workers=1).uploaded == 0
    assert len(client.uploads) == first_uploads

    chunk = next(o for o in plan.objects if o.phase == 0)
    size, _ = client.objects[f"aoi/store-v1/{chunk.key}"]
    client.objects[f"aoi/store-v1/{chunk.key}"] = (size, b64_md5(b"same size?"))
    with pytest.raises(PublishError, match="Refusing to overwrite"):
        upload(plan, adapter, workers=1)
    report = upload(plan, adapter, overwrite=True, workers=1)
    assert report.uploaded == 1
    assert client.uploads[-1]["key"] == f"aoi/store-v1/{chunk.key}"


def test_an_interrupted_upload_resumes_without_a_root_in_the_bucket(store, client, adapter):
    plan = plan_for(store, adapter)
    victim = [o for o in plan.objects if o.phase == 0][-1]
    client.fail_keys[f"aoi/store-v1/{victim.key}"] = api.ServiceUnavailable("flaky")
    with pytest.raises(PublishError, match=r"phase 1 \(chunks\).*ServiceUnavailable"):
        upload(plan, adapter, workers=1)
    assert "aoi/store-v1/zarr.json" not in client.objects
    del client.fail_keys[f"aoi/store-v1/{victim.key}"]
    report = upload(plan, adapter, workers=1)
    assert report.skipped > 0
    assert "aoi/store-v1/zarr.json" in client.objects


# -- CORS --------------------------------------------------------------------------------------

APP_RULE = {
    "origin": ["https://app.example.org"],
    "method": ["PUT"],
    "responseHeader": ["Content-Type"],
    "maxAgeSeconds": 60,
}


def cors_failing_then_passing():
    calls: list[int] = []

    def doctor(url: str) -> list[Check]:
        calls.append(1)
        if len(calls) == 1:
            return [Check("CORS on zarr.json", "fail", "no header", "set CORS")]
        return [Check("root zarr.json", "ok", "200")]

    return doctor, calls


def test_read_cors_returns_the_existing_rules(client, adapter):
    client.initial_cors = [APP_RULE]
    assert adapter.read_cors() == [APP_RULE]
    client.initial_cors = []
    assert adapter.read_cors() == []


def test_write_cors_sets_exactly_the_given_rules_and_patches_once(client, adapter):
    adapter.write_cors([APP_RULE, VIEWER_CORS_RULE])
    assert client.buckets["my-bucket"].patches == [[APP_RULE, VIEWER_CORS_RULE]]


def test_cors_permission_errors_name_the_permission(client, adapter):
    client.cors_read_error = api.Forbidden("no")
    with pytest.raises(PublishError, match=r"storage\.buckets\.get"):
        adapter.read_cors()
    client.cors_write_error = api.Forbidden("no")
    with pytest.raises(PublishError, match=r"storage\.buckets\.update"):
        adapter.write_cors([VIEWER_CORS_RULE])


def test_the_viewer_rule_is_what_doctor_needs_and_is_a_copy(adapter):
    rule = adapter.cors_rule()
    assert rule["origin"] == ["*"]
    assert {"GET", "HEAD"} <= set(rule["method"])
    # GCS uses one responseHeader list for both allow and expose.
    assert {"Range", "Content-Range"} <= set(rule["responseHeader"])
    rule["origin"].append("mutated")
    assert VIEWER_CORS_RULE["origin"] == ["*"]


def test_cors_is_only_read_without_apply_cors(store, client, adapter):
    client.initial_cors = [APP_RULE]
    doctor, _ = cors_failing_then_passing()
    result = publish(plan_for(store, adapter), adapter, workers=1, check_store=doctor)
    assert client.buckets["my-bucket"].patches == []
    assert not result.cors_changed
    assert result.link is None
    assert "--apply-cors" in result.notes[0]
    assert "1 CORS rule(s)" in result.notes[0]


def test_apply_cors_keeps_existing_rules_and_appends_the_viewer_rule(store, client, adapter):
    client.initial_cors = [APP_RULE]
    doctor, calls = cors_failing_then_passing()
    result = publish(
        plan_for(store, adapter), adapter, apply_cors=True, workers=1, check_store=doctor
    )
    assert client.buckets["my-bucket"].patches == [[APP_RULE, VIEWER_CORS_RULE]]
    assert result.cors_changed
    assert len(calls) == 2
    assert result.link is not None


def test_cors_is_not_touched_when_the_checks_pass(store, client, adapter):
    client.initial_cors = [APP_RULE]
    result = publish(
        plan_for(store, adapter), adapter, apply_cors=True, workers=1, check_store=passing
    )
    assert client.buckets["my-bucket"].patches == []
    assert result.link is not None


def test_an_unreadable_cors_configuration_is_reported_and_nothing_is_written(
    store, client, adapter
):
    client.cors_read_error = api.Forbidden("no")
    doctor, _ = cors_failing_then_passing()
    result = publish(
        plan_for(store, adapter), adapter, apply_cors=True, workers=1, check_store=doctor
    )
    assert client.buckets["my-bucket"].patches == []
    assert "storage.buckets.get" in result.notes[0]
    assert result.link is None


def test_access_help_offers_remedies_and_changes_nothing(adapter):
    text = adapter.access_help()
    assert "allUsers" in text
    assert "public access prevention" in text
    assert "--public-url" in text


# -- reading one object ------------------------------------------------------------------------


def test_read_object_returns_the_bytes_with_explicit_retry(client, adapter):
    client.contents["p/zarr.json"] = b'{"a": 1}'
    assert adapter.read_object("p/zarr.json") == b'{"a": 1}'
    (call,) = client.downloads
    assert call["key"] == "p/zarr.json"
    assert call["retry"] is DEFAULT_RETRY
    assert call["timeout"] > 0


def test_read_object_returns_none_for_a_missing_key(client, adapter):
    assert adapter.read_object("p/missing") is None


def test_read_object_names_the_permission_and_hides_the_provider_message(client, adapter):
    client.read_error = api.Forbidden("no access for user@example.org")
    with pytest.raises(PublishError, match=r"access denied.*storage\.objects\.get") as raised:
        adapter.read_object("p/zarr.json")
    assert "user@example.org" not in str(raised.value)


def test_read_object_maps_other_provider_errors_and_propagates_bugs(client, adapter):
    client.read_error = api.ServiceUnavailable("busy")
    with pytest.raises(PublishError, match=r"ServiceUnavailable.*busy"):
        adapter.read_object("p/zarr.json")
    client.read_error = RuntimeError("a bug")
    with pytest.raises(RuntimeError, match="a bug"):
        adapter.read_object("p/zarr.json")
