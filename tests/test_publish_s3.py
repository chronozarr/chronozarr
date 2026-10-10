"""The S3 and R2 adapter on botocore's Stubber: no request leaves the process."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from botocore.stub import ANY, Stubber

from chronozarr._publish_adapters import open_adapter
from chronozarr._publish_s3 import VIEWER_CORS_RULE, S3Adapter
from chronozarr.publish import (
    Destination,
    PublishError,
    plan_store,
    publish,
    upload,
)
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit

R2_ENDPOINT = "https://0123456789abcdef.r2.cloudflarestorage.com"


@pytest.fixture(autouse=True)
def isolated_aws(monkeypatch, tmp_path):
    """Dummy credentials and no config files, so nothing reads the developer's own setup."""
    for name in ("AWS_PROFILE", "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADUMMY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dummy-secret-value")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    path = tmp_path_factory.mktemp("publish_s3") / "store"
    build_store(path, make_truth(2, 2, 40, 50), shard=False, chunk_size=16)
    return path


def head(region: str | None) -> dict[str, Any]:
    headers = {"x-amz-bucket-region": region} if region else {}
    return {"ResponseMetadata": {"HTTPHeaders": headers}}


def client_error(code: str, message: str = "nope") -> dict[str, Any]:
    return {"service_error_code": code, "service_message": message, "http_status_code": 403}


# -- construction ------------------------------------------------------------------------------


def test_open_adapter_dispatches_on_scheme():
    adapter = open_adapter(Destination("s3", "bucket", "p"), region="eu-west-1")
    assert isinstance(adapter, S3Adapter)
    with pytest.raises(PublishError, match=r"unsupported destination scheme ftp://"):
        open_adapter(Destination("ftp", "bucket", "p"))


def test_r2_is_the_same_adapter_with_an_endpoint():
    adapter = S3Adapter("bucket", endpoint_url=R2_ENDPOINT)
    assert adapter.label == "Cloudflare R2"
    assert adapter.client.meta.region_name == "auto"
    assert adapter.client.meta.endpoint_url == R2_ENDPOINT
    assert adapter.client.meta.config.request_checksum_calculation == "when_required"
    description = adapter.endpoint_description()
    assert "0123456789abcdef.r2.cloudflarestorage.com" in description
    assert "not a browser address" in description


def test_aws_keeps_boto3_defaults_apart_from_retries_and_pool():
    adapter = S3Adapter("bucket", region="eu-west-1")
    config = adapter.client.meta.config
    assert adapter.label == "AWS S3"
    assert config.retries["mode"] == "standard"
    assert config.retries["total_max_attempts"] == 6
    assert config.request_checksum_calculation == "when_supported"
    assert "eu-west-1" in adapter.endpoint_description()


def test_other_s3_compatible_endpoints_are_labelled_as_such():
    adapter = S3Adapter("bucket", endpoint_url="https://minio.example.org", region="us-east-1")
    assert adapter.label == "S3-compatible storage"
    assert adapter.default_public_url("p") is None


@pytest.mark.parametrize(
    "endpoint",
    ["http://abc.r2.cloudflarestorage.com", "https://key:hunter2@abc.r2.cloudflarestorage.com"],
)
def test_endpoint_must_be_https_without_credentials(endpoint):
    with pytest.raises(PublishError, match="https URL without credentials") as raised:
        S3Adapter("bucket", endpoint_url=endpoint)
    assert "hunter2" not in str(raised.value)


def test_unknown_profile_is_a_clear_error():
    with pytest.raises(PublishError, match="profile 'nope' not found"):
        S3Adapter("bucket", profile="nope")


def test_missing_boto3_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(PublishError, match=r"uv sync --extra publish"):
        S3Adapter("bucket")


def test_the_endpoint_description_never_contains_credentials():
    adapter = S3Adapter("bucket", endpoint_url=R2_ENDPOINT)
    text = adapter.endpoint_description() + adapter.access_help()
    assert "AKIADUMMY" not in text
    assert "dummy-secret-value" not in text


# -- public URL --------------------------------------------------------------------------------


def test_aws_public_url_uses_the_buckets_own_region_and_encodes_the_prefix():
    adapter = S3Adapter("my-bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_response("head_bucket", head("us-west-2"), {"Bucket": "my-bucket"})
        url = adapter.default_public_url("aoi/store v1")
        stubber.assert_no_pending_responses()
    assert url == "https://my-bucket.s3.us-west-2.amazonaws.com/aoi/store%20v1"


def test_aws_public_url_falls_back_to_the_client_region_and_handles_dotted_buckets():
    adapter = S3Adapter("my.dotted.bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_response("head_bucket", head(None), {"Bucket": "my.dotted.bucket"})
        url = adapter.default_public_url("store")
    assert url == "https://s3.eu-west-1.amazonaws.com/my.dotted.bucket/store"


def test_aws_public_url_for_a_bucket_root_has_no_trailing_path():
    adapter = S3Adapter("my-bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_response("head_bucket", head("eu-west-1"), {"Bucket": "my-bucket"})
        assert adapter.default_public_url("") == "https://my-bucket.s3.eu-west-1.amazonaws.com"


def test_r2_has_no_default_public_url():
    adapter = S3Adapter("bucket", endpoint_url=R2_ENDPOINT)
    assert adapter.default_public_url("store") is None


# -- listing and storing -----------------------------------------------------------------------


def test_list_objects_follows_pages_and_strips_etag_quotes():
    adapter = S3Adapter("bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_response(
            "list_objects_v2",
            {
                "IsTruncated": True,
                "NextContinuationToken": "t1",
                "Contents": [{"Key": "a/b/zarr.json", "Size": 7, "ETag": '"ABCDEF"'}],
            },
            {"Bucket": "bucket", "Prefix": "a/b/"},
        )
        stubber.add_response(
            "list_objects_v2",
            {"IsTruncated": False, "Contents": [{"Key": "a/b/0/c", "Size": 3, "ETag": '"12"'}]},
            {"Bucket": "bucket", "Prefix": "a/b/", "ContinuationToken": "t1"},
        )
        found = adapter.list_objects("a/b")
        stubber.assert_no_pending_responses()
    assert {k: (v.size, v.etag) for k, v in found.items()} == {
        "a/b/zarr.json": (7, "abcdef"),
        "a/b/0/c": (3, "12"),
    }


def test_list_objects_at_the_bucket_root_lists_everything():
    adapter = S3Adapter("bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_response("list_objects_v2", {}, {"Bucket": "bucket", "Prefix": ""})
        assert adapter.list_objects("") == {}


def test_list_errors_say_which_permission_is_missing():
    adapter = S3Adapter("bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_client_error("list_objects_v2", **client_error("AccessDenied"))
        with pytest.raises(PublishError, match=r"access denied.*s3:ListBucket"):
            adapter.list_objects("p")
        stubber.add_client_error("list_objects_v2", **client_error("NoSuchBucket"))
        with pytest.raises(PublishError, match=r"was not found"):
            adapter.list_objects("p")


def test_rejected_credentials_do_not_echo_the_service_message():
    adapter = S3Adapter("bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_client_error(
            "list_objects_v2",
            **client_error("InvalidAccessKeyId", "The key AKIADUMMY does not exist"),
        )
        with pytest.raises(PublishError, match="rejected") as raised:
            adapter.list_objects("p")
    assert "AKIADUMMY" not in str(raised.value)


def test_upload_sends_chunks_then_metadata_then_root_with_headers_and_keys(store):
    adapter = S3Adapter("bucket", region="eu-west-1")
    dest = Destination("s3", "bucket", "aoi/store-v1")
    plan = plan_store(store, dest, adapter, "https://data.example.com/aoi/store-v1")
    with Stubber(adapter.client) as stubber:
        stubber.add_response(
            "list_objects_v2", {}, {"Bucket": "bucket", "Prefix": "aoi/store-v1/"}
        )
        for obj in plan.objects:
            stubber.add_response(
                "put_object",
                {"ETag": '"x"'},
                {
                    "Bucket": "bucket",
                    "Key": f"aoi/store-v1/{obj.key}",
                    "Body": ANY,
                    "CacheControl": obj.cache_control,
                    "ContentType": obj.content_type,
                },
            )
        report = upload(plan, adapter, workers=1)
        stubber.assert_no_pending_responses()
    assert report.uploaded == len(plan.objects)
    assert plan.objects[-1].key == "zarr.json"


def test_resume_skips_objects_the_bucket_already_holds(store):
    adapter = S3Adapter("bucket", region="eu-west-1")
    dest = Destination("s3", "bucket", "aoi/store-v1")
    plan = plan_store(store, dest, adapter, "https://data.example.com/aoi/store-v1")
    stored = plan.objects[:5]
    listing = {
        "Contents": [
            {"Key": f"aoi/store-v1/{o.key}", "Size": o.size, "ETag": '"deadbeef-3"'}
            for o in stored
        ]
    }
    remaining = plan.objects[5:]
    with Stubber(adapter.client) as stubber:
        stubber.add_response(
            "list_objects_v2", listing, {"Bucket": "bucket", "Prefix": "aoi/store-v1/"}
        )
        for obj in remaining:
            stubber.add_response(
                "put_object",
                {},
                {
                    "Bucket": "bucket",
                    "Key": f"aoi/store-v1/{obj.key}",
                    "Body": ANY,
                    "CacheControl": ANY,
                    "ContentType": ANY,
                },
            )
        report = upload(plan, adapter, workers=1)
        stubber.assert_no_pending_responses()
    assert (report.skipped, report.uploaded) == (5, len(remaining))


def test_a_put_failure_stops_the_phase_and_names_the_key(store):
    adapter = S3Adapter("bucket", region="eu-west-1")
    dest = Destination("s3", "bucket", "p")
    plan = plan_store(store, dest, adapter, "https://data.example.com/p")
    with Stubber(adapter.client) as stubber:
        stubber.add_response("list_objects_v2", {}, {"Bucket": "bucket", "Prefix": "p/"})
        stubber.add_client_error("put_object", **client_error("SlowDown", "reduce your rate"))
        with pytest.raises(PublishError, match=r"phase 1 \(data chunks\).*SlowDown"):
            upload(plan, adapter, workers=1)


# -- CORS --------------------------------------------------------------------------------------


def test_read_cors_returns_the_existing_rules_unchanged():
    adapter = S3Adapter("bucket", region="eu-west-1")
    rules = [
        {"ID": "app", "AllowedOrigins": ["https://app.example.org"], "AllowedMethods": ["PUT"]}
    ]
    with Stubber(adapter.client) as stubber:
        stubber.add_response("get_bucket_cors", {"CORSRules": rules}, {"Bucket": "bucket"})
        assert adapter.read_cors() == rules


def test_no_cors_configuration_reads_as_no_rules():
    adapter = S3Adapter("bucket", region="eu-west-1")
    with Stubber(adapter.client) as stubber:
        stubber.add_client_error("get_bucket_cors", **client_error("NoSuchCORSConfiguration"))
        assert adapter.read_cors() == []


def test_cors_permission_errors_name_the_permission_and_the_r2_remedy():
    aws = S3Adapter("bucket", region="eu-west-1")
    with Stubber(aws.client) as stubber:
        stubber.add_client_error("get_bucket_cors", **client_error("AccessDenied"))
        with pytest.raises(PublishError, match="s3:GetBucketCORS") as raised:
            aws.read_cors()
    assert "wrangler" not in str(raised.value)
    r2 = S3Adapter("bucket", endpoint_url=R2_ENDPOINT)
    with Stubber(r2.client) as stubber:
        stubber.add_client_error("put_bucket_cors", **client_error("AccessDenied"))
        with pytest.raises(PublishError, match="s3:PutBucketCORS") as raised:
            r2.write_cors([VIEWER_CORS_RULE])
    assert "cannot manage CORS" in str(raised.value)
    assert "wrangler r2 bucket cors set" in str(raised.value)


def test_write_cors_replaces_with_exactly_the_rules_it_is_given():
    adapter = S3Adapter("bucket", region="eu-west-1")
    rules = [
        {"AllowedOrigins": ["https://app.example.org"], "AllowedMethods": ["PUT"]},
        VIEWER_CORS_RULE,
    ]
    with Stubber(adapter.client) as stubber:
        stubber.add_response(
            "put_bucket_cors", {}, {"Bucket": "bucket", "CORSConfiguration": {"CORSRules": rules}}
        )
        adapter.write_cors(rules)
        stubber.assert_no_pending_responses()


def test_the_viewer_rule_is_what_doctor_needs():
    rule = S3Adapter("bucket", region="eu-west-1").cors_rule()
    assert rule["AllowedOrigins"] == ["*"]
    assert {"GET", "HEAD"} <= set(rule["AllowedMethods"])
    assert "Range" in rule["AllowedHeaders"]
    assert "Content-Range" in rule["ExposeHeaders"]
    rule["AllowedOrigins"].append("mutated")
    assert VIEWER_CORS_RULE["AllowedOrigins"] == ["*"]


def test_publish_preserves_existing_cors_through_the_s3_adapter(store):
    from chronozarr.doctor import Check

    adapter = S3Adapter("bucket", region="eu-west-1")
    dest = Destination("s3", "bucket", "p")
    plan = plan_store(store, dest, adapter, "https://data.example.com/p")
    existing = [
        {"ID": "app", "AllowedOrigins": ["https://app.example.org"], "AllowedMethods": ["PUT"]}
    ]
    seen: list[int] = []

    def doctor(url: str) -> list[Check]:
        seen.append(1)
        if len(seen) == 1:
            return [Check("CORS on zarr.json", "fail", "no header", "set CORS")]
        return [Check("root zarr.json", "ok", "200")]

    with Stubber(adapter.client) as stubber:
        stubber.add_response("list_objects_v2", {}, {"Bucket": "bucket", "Prefix": "p/"})
        for obj in plan.objects:
            stubber.add_response(
                "put_object",
                {},
                {
                    "Bucket": "bucket",
                    "Key": f"p/{obj.key}",
                    "Body": ANY,
                    "CacheControl": ANY,
                    "ContentType": ANY,
                },
            )
        stubber.add_response("get_bucket_cors", {"CORSRules": existing}, {"Bucket": "bucket"})
        stubber.add_response(
            "put_bucket_cors",
            {},
            {
                "Bucket": "bucket",
                "CORSConfiguration": {"CORSRules": [*existing, VIEWER_CORS_RULE]},
            },
        )
        result = publish(plan, adapter, apply_cors=True, workers=1, check_store=doctor)
        stubber.assert_no_pending_responses()
    assert result.cors_changed
    assert result.link is not None
