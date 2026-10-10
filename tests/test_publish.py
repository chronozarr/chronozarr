"""`chronozarr publish`: planning, upload order, resume, overwrite refusal, CORS and the link.

The storage provider is a fake adapter that writes into a directory. Verification runs the real
doctor against a local range-capable HTTP server that serves that directory.
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

import chronozarr._publish_adapters as adapters_module
from chronozarr.cli import main
from chronozarr.doctor import Check, Status
from chronozarr.publish import (
    IMMUTABLE,
    PHASES,
    SHORT_TTL,
    Destination,
    PublishError,
    RemoteObject,
    check_public_url,
    inspect_destination,
    merged_cors,
    parse_destination,
    plan_store,
    publish,
    upload,
)
from chronozarr.view import VIEWER_URL, StoreRequestHandler
from tests.synthetic import build_store, make_truth
from tests.test_doctor import NoCorsHandler, serving

pytestmark = pytest.mark.unit

REQUIRED_RULE = {"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "HEAD"], "tag": "viewer"}
DEST = Destination("s3", "bucket", "aoi/store-v1")
PUBLIC = "https://data.example.com/aoi/store-v1"


class FakeAdapter:
    """A bucket in a directory. Records every call so tests can check order and arguments."""

    label = "Fake storage"

    def __init__(self, root: Path, *, default_url: str | None = None, cors=None):
        self.root = root
        self.default_url = default_url
        self.cors: list[dict[str, Any]] | None = cors
        self.cors_read_error: PublishError | None = None
        self.puts: list[str] = []
        self.headers: dict[str, tuple[str, str]] = {}
        self.fail_keys: set[str] = set()
        self.cors_writes: list[list[dict[str, Any]]] = []
        self.listed = False
        root.mkdir(parents=True, exist_ok=True)

    def endpoint_description(self) -> str:
        return "a fake endpoint"

    def default_public_url(self, prefix: str) -> str | None:
        return self.default_url

    def list_objects(self, prefix: str) -> dict[str, RemoteObject]:
        self.listed = True
        found = {}
        for path in sorted(self.root.rglob("*")):
            key = path.relative_to(self.root).as_posix()
            if path.is_file() and key.startswith(f"{prefix}/" if prefix else ""):
                data = path.read_bytes()
                found[key] = RemoteObject(len(data), hashlib.md5(data).hexdigest())
        return found

    def put_object(self, key: str, path: Path, *, cache_control: str, content_type: str) -> None:
        if key in self.fail_keys:
            raise OSError(f"connection reset while sending {key}")
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        self.puts.append(key)
        self.headers[key] = (cache_control, content_type)

    def read_cors(self) -> list[dict[str, Any]]:
        if self.cors_read_error:
            raise self.cors_read_error
        return list(self.cors or [])

    def write_cors(self, rules: list[dict[str, Any]]) -> None:
        self.cors_writes.append(rules)
        self.cors = rules

    def cors_rule(self) -> dict[str, Any]:
        return dict(REQUIRED_RULE)

    def access_help(self) -> str:
        return "make the bucket readable"


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("publish") / "store"
    build_store(path, make_truth(3, 2, 40, 50), shard=False, chunk_size=16)
    return path


@pytest.fixture
def adapter(tmp_path) -> FakeAdapter:
    return FakeAdapter(tmp_path / "bucket")


def make_plan(store: Path, adapter: FakeAdapter, url: str | None = PUBLIC):
    return plan_store(store, DEST, adapter, url)


def ok_checks(*_: Any) -> list[Check]:
    return [Check("root zarr.json", "ok", "200")]


def failing(name: str, status: Status = "fail") -> list[Check]:
    return [Check(name, status, "bad", "do the thing")]


# -- destination and public URL ----------------------------------------------------------------


def test_destination_parses_bucket_and_prefix():
    parsed = parse_destination("s3://my-bucket/aoi/store-v1/")
    assert (parsed.scheme, parsed.bucket, parsed.prefix) == ("s3", "my-bucket", "aoi/store-v1")
    assert parsed.uri == "s3://my-bucket/aoi/store-v1"
    assert parsed.key("0/zarr.json") == "aoi/store-v1/0/zarr.json"
    assert parse_destination("s3://my-bucket").key("zarr.json") == "zarr.json"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("https://data.example.com/store", "web URL"),
        ("my-bucket/store", "not a storage location"),
        ("s3://my-bucket/a/../b", "'..' segment"),
        ("s3://my-bucket/a//b", "empty"),
        ("s3://key:secret@my-bucket/store", "no credentials"),
        ("s3://my-bucket/store?versionId=1", "no credentials"),
    ],
)
def test_destination_rejects_what_is_not_a_storage_location(text, message):
    with pytest.raises(PublishError, match=message) as raised:
        parse_destination(text)
    assert "secret" not in str(raised.value)


def test_a_web_url_destination_points_at_public_url_and_hides_credentials():
    with pytest.raises(PublishError, match="--public-url") as raised:
        parse_destination("https://key:secret@data.example.com/store?sig=abc")
    assert "secret" not in str(raised.value)
    assert "sig=abc" not in str(raised.value)


def test_public_url_is_normalised_and_must_be_browser_safe():
    assert check_public_url("https://data.example.com/aoi/") == "https://data.example.com/aoi"
    assert check_public_url("http://127.0.0.1:8000/store") == "http://127.0.0.1:8000/store"
    for bad, message in [
        ("s3://bucket/prefix", "not an https URL"),
        ("https://data.example.com/s?X-Amz-Signature=abc", "credentials, a query"),
        ("https://key:secret@data.example.com/s", "credentials, a query"),
        ("https://data.example.com/s#frag", "credentials, a query"),
        ("http://data.example.com/s", "plain http"),
    ]:
        with pytest.raises(PublishError, match=message) as raised:
            check_public_url(bad)
        assert "secret" not in str(raised.value)
        assert "Signature" not in str(raised.value)


# -- plan --------------------------------------------------------------------------------------


def test_plan_lists_objects_in_phase_order_with_cache_headers(store, adapter):
    plan = make_plan(store, adapter)
    phases = [o.phase for o in plan.objects]
    assert phases == sorted(phases)
    assert plan.objects[-1].key == "zarr.json"
    assert {o.key for o in plan.objects if o.phase == 3} == {"zarr.json"}
    assert all(o.key.endswith("/zarr.json") for o in plan.objects if o.phase == 2)
    assert {o.key for o in plan.objects if o.phase == 1} >= {"0/time/c/0", "volatility/c/0/0"}
    assert all(o.cache_control == SHORT_TTL for o in plan.objects if o.phase)
    assert not any(o.key.endswith("zarr.json") for o in plan.objects if o.phase < 2)

    cache = {o.key: o.cache_control for o in plan.objects}
    assert cache["zarr.json"] == SHORT_TTL
    assert cache["0/data/zarr.json"] == SHORT_TTL
    assert cache["0/time/c/0"] == SHORT_TTL
    assert cache["0/data/c/0/0/0/0"] == IMMUTABLE
    assert plan.total_bytes == sum(p.stat().st_size for p in store.rglob("*") if p.is_file())
    assert plan.objects[0].content_type == "application/octet-stream"
    assert plan.objects[-1].content_type == "application/json"


def test_plan_summary_shows_destination_count_bytes_and_distinguishes_the_public_url(
    store, adapter
):
    plan = make_plan(store, adapter)
    text = plan.summary(adapter, "empty")
    assert "s3://bucket/aoi/store-v1" in text
    assert f"{len(plan.objects):,} objects" in text or f"{len(plan.objects):,}," in text
    assert f"{plan.total_bytes:,} bytes" in text
    assert PUBLIC in text
    for name in PHASES:
        assert name in text
    assert IMMUTABLE in text
    assert "derived" not in text


def test_plan_without_a_public_url_asks_the_adapter_and_says_so(store, tmp_path):
    adapter = FakeAdapter(
        tmp_path / "b", default_url="https://bucket.s3.us-west-2.amazonaws.com/x"
    )
    plan = make_plan(store, adapter, None)
    assert plan.public_url == "https://bucket.s3.us-west-2.amazonaws.com/x"
    assert "derived from the Fake storage endpoint" in plan.summary(adapter)


def test_plan_without_a_public_url_fails_when_the_provider_has_none(store, adapter):
    with pytest.raises(PublishError, match=r"--public-url is required.*not a browser address"):
        make_plan(store, adapter, None)


def test_an_s3_uri_is_never_taken_as_the_public_url(store, adapter):
    with pytest.raises(PublishError, match="not an https URL"):
        make_plan(store, adapter, "s3://bucket/aoi/store-v1")


def test_plan_validates_the_store_before_asking_the_provider_anything(tmp_path):
    class Untouchable(FakeAdapter):
        def default_public_url(self, prefix):
            raise AssertionError("asked the provider before validating")

    adapter = Untouchable(tmp_path / "b")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PublishError, match="not a chronozarr store"):
        plan_store(empty, DEST, adapter, None)
    assert not adapter.listed


def test_plan_refuses_a_store_that_fails_validation(store, adapter, tmp_path):
    broken = tmp_path / "broken"
    shutil.copytree(store, broken)
    (broken / "0" / "data" / "zarr.json").write_text("{}")
    with pytest.raises(PublishError, match=r"could not be read.*nothing was uploaded"):
        make_plan(broken, adapter)
    assert adapter.puts == []


def test_plan_lists_validation_problems_and_stays_offline(store, adapter, monkeypatch):
    monkeypatch.setattr(
        "chronozarr.publish.schema.validate", lambda path: [f"problem {i}" for i in range(12)]
    )
    with pytest.raises(PublishError, match=r"(?s)does not conform.*problem 0.*2 more") as raised:
        make_plan(store, adapter)
    assert "problem 10" not in str(raised.value)
    assert not adapter.listed


def test_plan_skips_hidden_files_and_reports_them(store, adapter, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(store, copy)
    (copy / ".DS_Store").write_bytes(b"junk")
    plan = make_plan(copy, adapter)
    assert ".DS_Store" not in {o.key for o in plan.objects}
    assert "skipped      1 hidden file(s)" in plan.summary(adapter)


# -- upload: order, headers, resume, overwrite -------------------------------------------------


def test_upload_stores_chunks_then_metadata_then_the_root_with_headers(store, adapter):
    plan = make_plan(store, adapter)
    report = upload(plan, adapter, workers=4)
    assert report.uploaded == len(plan.objects)
    assert report.skipped == 0
    assert report.uploaded_bytes == plan.total_bytes

    prefix = "aoi/store-v1/"
    order = [key.removeprefix(prefix) for key in adapter.puts]
    assert len(order) == len(set(order)) == len(plan.objects)
    phases = {o.key: o.phase for o in plan.objects}
    assert [phases[k] for k in order] == sorted(phases[k] for k in order)
    assert order[-1] == "zarr.json"
    assert adapter.headers[f"{prefix}zarr.json"] == (SHORT_TTL, "application/json")
    assert adapter.headers[f"{prefix}0/data/c/0/0/0/0"] == (IMMUTABLE, "application/octet-stream")
    assert all(key.startswith(prefix) for key in adapter.puts)


def test_a_failed_phase_stops_before_metadata_and_a_rerun_resumes(store, adapter):
    plan = make_plan(store, adapter)
    broken = "aoi/store-v1/0/data/c/1/0/1/1"
    adapter.fail_keys = {broken}
    with pytest.raises(
        PublishError, match=r"phase 1 .data chunks.*Run the same command again"
    ) as raised:
        upload(plan, adapter, workers=1)
    assert broken in str(raised.value)
    stored = set(adapter.list_objects("aoi/store-v1"))
    assert "aoi/store-v1/zarr.json" not in stored
    assert not any(k.endswith("/zarr.json") for k in stored)
    first_run = set(adapter.puts)
    assert 0 < len(first_run) < len(plan.objects)

    adapter.fail_keys = set()
    adapter.puts.clear()
    report = upload(plan, adapter, workers=4)
    assert report.skipped == len(first_run)
    assert report.uploaded == len(plan.objects) - len(first_run)
    assert not first_run & set(adapter.puts)
    assert adapter.puts[-1] == "aoi/store-v1/zarr.json"

    adapter.puts.clear()
    again = upload(plan, adapter)
    assert (again.uploaded, again.skipped) == (0, len(plan.objects))
    assert adapter.puts == []


def test_rerun_after_a_complete_upload_changes_nothing(store, adapter):
    plan = make_plan(store, adapter)
    upload(plan, adapter)
    adapter.puts.clear()
    logged: list[str] = []
    upload(plan, adapter, log=logged.append)
    assert adapter.puts == []
    assert any("resuming" in line for line in logged)


def test_a_prefix_holding_foreign_objects_is_refused_without_writing(store, adapter):
    plan = make_plan(store, adapter)
    stray = adapter.root / "aoi/store-v1/notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("someone else's file")
    with pytest.raises(PublishError, match=r"Refusing to overwrite.*fresh prefix.*--overwrite"):
        upload(plan, adapter)
    assert adapter.puts == []


def test_a_different_object_under_the_same_key_is_refused(store, adapter):
    plan = make_plan(store, adapter)
    upload(plan, adapter)
    existing = adapter.root / "aoi/store-v1/0/data/c/0/0/0/0"
    existing.write_bytes(existing.read_bytes() + b"x")
    adapter.puts.clear()
    with pytest.raises(PublishError, match="1 that differ"):
        upload(plan, adapter)
    assert adapter.puts == []


def test_same_size_but_different_checksum_counts_as_different(store, adapter):
    plan = make_plan(store, adapter)
    upload(plan, adapter)
    existing = adapter.root / "aoi/store-v1/0/data/c/0/0/0/0"
    data = bytearray(existing.read_bytes())
    data[0] ^= 0xFF
    existing.write_bytes(bytes(data))
    with pytest.raises(PublishError, match="1 that differ"):
        upload(plan, adapter)


def test_an_etag_that_is_not_an_md5_falls_back_to_size(store, adapter):
    plan = make_plan(store, adapter)
    upload(plan, adapter)

    class Multipart(FakeAdapter):
        def list_objects(self, prefix):
            return {
                k: RemoteObject(v.size, "abc123-7")
                for k, v in super().list_objects(prefix).items()
            }

    other = Multipart(adapter.root)
    state = inspect_destination(plan, other)
    assert len(state.identical) == len(plan.objects)
    assert not state.conflicts


def test_overwrite_replaces_differing_objects_and_deletes_nothing(store, adapter):
    plan = make_plan(store, adapter)
    upload(plan, adapter)
    changed = adapter.root / "aoi/store-v1/0/data/c/0/0/0/0"
    changed.write_bytes(b"stale")
    stray = adapter.root / "aoi/store-v1/notes.txt"
    stray.write_text("keep me")
    adapter.puts.clear()
    report = upload(plan, adapter, overwrite=True)
    assert report.uploaded == 1
    assert adapter.puts == ["aoi/store-v1/0/data/c/0/0/0/0"]
    assert changed.read_bytes() == (store / "0/data/c/0/0/0/0").read_bytes()
    assert stray.read_text() == "keep me"


def test_destination_state_describes_each_case(store, adapter):
    plan = make_plan(store, adapter)
    state = inspect_destination(plan, adapter)
    assert state.describe(False).startswith("empty")
    adapter.fail_keys = {"aoi/store-v1/zarr.json"}
    with pytest.raises(PublishError):
        upload(plan, adapter)
    assert "will resume" in inspect_destination(plan, adapter).describe(False)
    (adapter.root / "aoi/store-v1/extra").write_text("x")
    conflicted = inspect_destination(plan, adapter)
    assert "refused without --overwrite" in conflicted.describe(False)
    assert "deletes nothing" in conflicted.describe(True)


def test_a_prefix_that_only_shares_a_name_prefix_is_not_listed(store, adapter):
    sibling = adapter.root / "aoi/store-v10/zarr.json"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("{}")
    plan = make_plan(store, adapter)
    assert inspect_destination(plan, adapter).foreign == frozenset()
    assert upload(plan, adapter).uploaded == len(plan.objects)


# -- verification and the link -----------------------------------------------------------------


def test_publish_returns_a_correctly_encoded_link_after_doctor_passes(store, tmp_path):
    bucket = tmp_path / "bucket"
    served = bucket / "store-v1"
    served.mkdir(parents=True)
    adapter = FakeAdapter(bucket)
    with serving(StoreRequestHandler, served) as url:
        plan = plan_store(store, Destination("s3", "bucket", "store-v1"), adapter, url)
        result = publish(plan, adapter, workers=4)
    assert [c for c in result.checks if c.status == "fail"] == []
    assert result.link == f"{VIEWER_URL}?store={urllib.parse.quote(url, safe='')}"
    assert result.link.startswith("https://chronozarr.org/demo/?store=http%3A%2F%2F127.0.0.1%3A")
    assert result.report.uploaded == len(plan.objects)
    assert not result.cors_changed


def test_doctor_failure_on_the_hosted_store_gives_no_link(store, tmp_path):
    bucket = tmp_path / "bucket"
    served = bucket / "store-v1"
    served.mkdir(parents=True)
    adapter = FakeAdapter(bucket)
    with serving(NoCorsHandler, served) as url:
        plan = plan_store(store, Destination("s3", "bucket", "store-v1"), adapter, url)
        result = publish(plan, adapter, workers=4)
    assert result.link is None
    assert any(c.status == "fail" and c.name.startswith("CORS") for c in result.checks)
    assert adapter.cors_writes == []
    assert result.report.uploaded == len(plan.objects)


def test_an_unreachable_public_url_gives_no_link(store, adapter):
    plan = make_plan(store, adapter, "http://127.0.0.1:9/never")
    result = publish(plan, adapter, workers=4)
    assert result.link is None
    assert any(c.status == "fail" for c in result.checks)


def test_warnings_do_not_block_the_link(store, adapter):
    plan = make_plan(store, adapter)
    result = publish(
        plan, adapter, check_store=lambda url: [*ok_checks(), *failing("cache-control", "warn")]
    )
    assert result.link is not None
    assert result.link.startswith(
        "https://chronozarr.org/demo/?store=https%3A%2F%2Fdata.example.com"
    )


def test_the_link_encodes_unusual_characters_in_the_public_url(store, adapter):
    plan = make_plan(store, adapter, "https://data.example.com/a b/ü&x=1/store")
    result = publish(plan, adapter, check_store=ok_checks)
    assert result.link == f"{VIEWER_URL}?store=" + urllib.parse.quote(
        "https://data.example.com/a b/ü&x=1/store", safe=""
    )
    assert "&x=1" not in result.link
    assert " " not in result.link


def test_a_non_cors_failure_gives_no_link_even_with_apply_cors(store, adapter):
    plan = make_plan(store, adapter)
    result = publish(
        plan, adapter, apply_cors=True, check_store=lambda url: failing("root zarr.json")
    )
    assert result.link is None
    assert adapter.cors_writes == []


def test_upload_failure_raises_before_any_verification(store, adapter):
    plan = make_plan(store, adapter)
    adapter.fail_keys = {"aoi/store-v1/zarr.json"}

    def forbidden(url: str) -> list[Check]:
        raise AssertionError("verified an incomplete upload")

    with pytest.raises(PublishError, match=r"phase 4 .root metadata"):
        publish(plan, adapter, check_store=forbidden)


# -- CORS --------------------------------------------------------------------------------------


EXISTING = [
    {"AllowedOrigins": ["https://app.example.org"], "AllowedMethods": ["PUT"], "ID": "app"}
]


def cors_then_ok(calls: list[str]):
    def check(url: str) -> list[Check]:
        calls.append(url)
        return failing("CORS on zarr.json") if len(calls) == 1 else ok_checks()

    return check


def test_cors_is_only_read_without_apply_cors(store, tmp_path):
    adapter = FakeAdapter(tmp_path / "b", cors=list(EXISTING))
    plan = make_plan(store, adapter)
    calls: list[str] = []
    result = publish(plan, adapter, check_store=cors_then_ok(calls))
    assert result.link is None
    assert len(calls) == 1
    assert adapter.cors_writes == []
    assert adapter.cors == EXISTING
    assert any("--apply-cors" in line and "1 CORS rule" in line for line in result.notes)


def test_apply_cors_keeps_existing_rules_and_appends_the_viewer_rule(store, tmp_path):
    adapter = FakeAdapter(tmp_path / "b", cors=list(EXISTING))
    plan = make_plan(store, adapter)
    calls: list[str] = []
    result = publish(plan, adapter, apply_cors=True, check_store=cors_then_ok(calls))
    assert adapter.cors_writes == [[*EXISTING, REQUIRED_RULE]]
    assert result.cors_changed
    assert len(calls) == 2
    assert result.link is not None


def test_apply_cors_that_does_not_fix_the_failure_gives_no_link(store, tmp_path):
    adapter = FakeAdapter(tmp_path / "b", cors=list(EXISTING))
    plan = make_plan(store, adapter)
    result = publish(
        plan, adapter, apply_cors=True, check_store=lambda url: failing("CORS on zarr.json")
    )
    assert result.cors_changed
    assert result.link is None


def test_cors_is_not_touched_when_the_checks_pass(store, tmp_path):
    adapter = FakeAdapter(tmp_path / "b", cors=list(EXISTING))
    plan = make_plan(store, adapter)
    result = publish(plan, adapter, apply_cors=True, check_store=ok_checks)
    assert adapter.cors_writes == []
    assert not result.cors_changed
    assert result.link is not None


def test_cors_is_not_rewritten_when_the_viewer_rule_is_already_there(store, tmp_path):
    adapter = FakeAdapter(tmp_path / "b", cors=[*EXISTING, REQUIRED_RULE])
    plan = make_plan(store, adapter)
    result = publish(
        plan,
        adapter,
        apply_cors=True,
        check_store=lambda url: failing("CORS on zarr.json"),
    )
    assert adapter.cors_writes == []
    assert result.link is None
    assert any("already has the viewer rule" in line for line in result.notes)


def test_unreadable_cors_is_reported_and_nothing_is_written(store, tmp_path):
    adapter = FakeAdapter(tmp_path / "b")
    adapter.cors_read_error = PublishError("access denied: the credentials need s3:GetBucketCORS")
    plan = make_plan(store, adapter)
    result = publish(
        plan,
        adapter,
        apply_cors=True,
        check_store=lambda url: failing("CORS on zarr.json"),
    )
    assert adapter.cors_writes == []
    assert result.link is None
    assert any(
        "could not read the bucket's CORS" in line and "GetBucketCORS" in line
        for line in result.notes
    )


def test_merged_cors_appends_last_and_is_idempotent():
    rule = {"AllowedOrigins": ["*"]}
    assert merged_cors([], rule) == [rule]
    assert merged_cors(EXISTING, rule) == [*EXISTING, rule]
    assert merged_cors([*EXISTING, rule], rule) == [*EXISTING, rule]


# -- command line ------------------------------------------------------------------------------


@pytest.fixture
def cli_adapter(tmp_path, monkeypatch) -> FakeAdapter:
    fake = FakeAdapter(tmp_path / "bucket")
    monkeypatch.setattr(adapters_module, "open_adapter", lambda destination, **options: fake)
    return fake


def run(*args: str):
    return CliRunner().invoke(main, ["publish", *args], catch_exceptions=False)


def test_cli_dry_run_prints_the_plan_and_uploads_nothing(store, cli_adapter):
    result = run(
        str(store),
        "--destination",
        "s3://bucket/aoi/store-v1",
        "--public-url",
        PUBLIC,
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert "s3://bucket/aoi/store-v1" in result.output
    assert PUBLIC in result.output
    assert "prefix       empty" in result.output
    assert "dry run: nothing was uploaded" in result.output
    assert cli_adapter.puts == []


def test_cli_dry_run_reports_a_prefix_that_would_be_refused(store, cli_adapter):
    stray = cli_adapter.root / "aoi/store-v1/notes.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    result = run(
        str(store),
        "--destination",
        "s3://bucket/aoi/store-v1",
        "--public-url",
        PUBLIC,
        "--dry-run",
    )
    assert result.exit_code == 1
    assert "refused without --overwrite" in result.output
    assert "would be refused" in result.output


def test_cli_publishes_and_prints_the_link_last(store, tmp_path, monkeypatch):
    bucket = tmp_path / "bucket"
    served = bucket / "store-v1"
    served.mkdir(parents=True)
    fake = FakeAdapter(bucket)
    monkeypatch.setattr(adapters_module, "open_adapter", lambda destination, **options: fake)
    with serving(StoreRequestHandler, served) as url:
        result = run(str(store), "--destination", "s3://bucket/store-v1", "--public-url", url)
    assert result.exit_code == 0, result.output
    link = f"{VIEWER_URL}?store={urllib.parse.quote(url, safe='')}"
    assert link in result.output
    assert result.output.index("[ ok ]") < result.output.index(link)
    assert "charges are yours" in result.output
    assert fake.puts[-1] == "store-v1/zarr.json"


def test_cli_failed_verification_exits_1_and_prints_no_link(store, tmp_path, monkeypatch):
    bucket = tmp_path / "bucket"
    served = bucket / "store-v1"
    served.mkdir(parents=True)
    fake = FakeAdapter(bucket)
    monkeypatch.setattr(adapters_module, "open_adapter", lambda destination, **options: fake)
    with serving(NoCorsHandler, served) as url:
        result = CliRunner().invoke(
            main,
            ["publish", str(store), "--destination", "s3://bucket/store-v1", "--public-url", url],
        )
    assert result.exit_code == 1
    assert "[FAIL]" in result.output
    assert "not verified" in result.output
    assert "chronozarr.org/demo" not in result.output
    assert len(fake.puts) > 0


def test_cli_explains_a_missing_public_url_and_rejects_an_s3_one(store, cli_adapter):
    missing = CliRunner().invoke(
        main, ["publish", str(store), "--destination", "s3://bucket/store-v1"]
    )
    assert missing.exit_code == 1
    assert "--public-url is required" in missing.output
    wrong = CliRunner().invoke(
        main,
        ["publish", str(store), "--destination", "s3://bucket/x", "--public-url", "s3://bucket/x"],
    )
    assert wrong.exit_code == 1
    assert "not an https URL" in wrong.output
    assert cli_adapter.puts == []


def test_cli_apply_cors_adds_the_rule_then_verifies_again(store, cli_adapter, monkeypatch):
    cli_adapter.cors = list(EXISTING)
    calls: list[str] = []

    def fake_diagnose(url: str, **_: Any) -> list[Check]:
        return cors_then_ok(calls)(url)

    monkeypatch.setattr("chronozarr.publish.diagnose", fake_diagnose)
    args = ["--destination", "s3://bucket/aoi/store-v1", "--public-url", PUBLIC]

    without = run(str(store), *args)
    assert without.exit_code == 1
    assert cli_adapter.cors_writes == []
    assert "Pass --apply-cors" in without.output
    assert "chronozarr.org/demo" not in without.output

    calls.clear()
    with_flag = run(str(store), *args, "--apply-cors")
    assert with_flag.exit_code == 0, with_flag.output
    assert cli_adapter.cors_writes == [[*EXISTING, REQUIRED_RULE]]
    assert "added the viewer CORS rule after the bucket's 1 existing rule(s)" in with_flag.output
    assert "chronozarr.org/demo/?store=https%3A%2F%2Fdata.example.com" in with_flag.output
