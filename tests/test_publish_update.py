"""`chronozarr publish --update`: only new and changed objects, order, resume, refusals, the link.

Storage is a fake adapter that writes into a directory; no cloud is contacted. The end-to-end test
verifies against the real doctor on a local range-capable HTTP server that serves that directory.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import urllib.parse
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from botocore.stub import Stubber
from click.testing import CliRunner

import chronozarr
import chronozarr._publish_adapters as adapters_module
from chronozarr._publish_s3 import S3Adapter
from chronozarr.append import append
from chronozarr.cli import main
from chronozarr.doctor import Check
from chronozarr.publish import (
    IMMUTABLE,
    SHORT_TTL,
    Destination,
    PublishError,
    plan_store,
    publish,
    upload,
)
from chronozarr.publish_update import (
    HostedRoot,
    ReadableStorage,
    hosted_root_checks,
    plan_update,
    publish_update,
)
from chronozarr.view import VIEWER_URL, StoreRequestHandler
from tests.synthetic import BANDS, make_da, make_truth
from tests.test_doctor import serving
from tests.test_publish import PUBLIC, FakeAdapter

pytestmark = pytest.mark.unit

CHUNK = 16
HEIGHT, WIDTH = 50, 70
N_TIME = 8
DEST = Destination("s3", "bucket", "aoi/store-v1")
PREFIX = "aoi/store-v1/"


class ReadingAdapter(FakeAdapter):
    """The fake bucket plus the one read `--update` needs."""

    def read_object(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.is_file() else None


def ok_checks(url: str) -> list[Check]:
    return [Check("fake doctor", "ok", url)]


def fetch_times(n: int, control: str = SHORT_TTL):
    return lambda url: HostedRoot(n, control)


@pytest.fixture(scope="module")
def truth() -> np.ndarray:
    return make_truth(N_TIME, 2, HEIGHT, WIDTH)


def encode_head(truth: np.ndarray, out: Path, n: int, **options) -> Path:
    options.setdefault("volatility", True)
    chronozarr.encode(make_da(truth[:n], BANDS), out, chunk_size=CHUNK, **options)
    return out


def appended(source: Path, out: Path, truth: np.ndarray, lo: int, hi: int) -> Path:
    shutil.copytree(source, out)
    append(out, make_da(truth, BANDS).isel(time=slice(lo, hi)))
    return out


@pytest.fixture(scope="module")
def stores(tmp_path_factory, truth) -> dict[str, Path]:
    """The unsharded store at 4 timesteps (`v1`) and after appending 2 (`v2`)."""
    base = tmp_path_factory.mktemp("update")
    v1 = encode_head(truth, base / "v1", 4)
    return {"v1": v1, "v2": appended(v1, base / "v2", truth, 4, 6)}


@pytest.fixture
def bucket(tmp_path) -> ReadingAdapter:
    return ReadingAdapter(tmp_path / "bucket")


def plan_for(store: Path, adapter: FakeAdapter, url: str = PUBLIC):
    return plan_store(store, DEST, adapter, url)


def local_digests(store: Path) -> dict[str, str]:
    return {
        p.relative_to(store).as_posix(): hashlib.md5(p.read_bytes()).hexdigest()
        for p in store.rglob("*")
        if p.is_file()
    }


def hosted_digests(adapter: ReadingAdapter) -> dict[str, str]:
    return {
        k.removeprefix(PREFIX): o.etag for k, o in adapter.list_objects("aoi/store-v1").items()
    }


def publish_first(store: Path, adapter: ReadingAdapter) -> None:
    upload(plan_for(store, adapter), adapter, workers=4)
    adapter.puts.clear()


# -- what is selected --------------------------------------------------------------------------


def test_only_new_and_changed_objects_are_selected(stores, bucket):
    publish_first(stores["v1"], bucket)
    before, after = local_digests(stores["v1"]), local_digests(stores["v2"])
    new = set(after) - set(before)
    changed = {k for k in before if before[k] != after[k]}

    update = plan_update(plan_for(stores["v2"], bucket), bucket)

    assert {o.key for o in update.new} == new
    assert {o.key for o in update.changed} == changed
    assert update.unchanged == len(before) - len(changed)
    assert (update.hosted_times, update.local_times) == (4, 6)
    # An unsharded append rewrites metadata and the two mutable chunk kinds, nothing else.
    assert {"zarr.json", "0/time/c/0", "volatility/c/0/0"} <= changed
    assert all(
        k.endswith("zarr.json") or "/time/" in k or k.startswith("volatility/") for k in changed
    )
    assert any(k.startswith("0/data/c/4/") for k in new)
    assert not any(k.startswith("0/data/c/0/") for k in new | changed)


def test_update_uploads_exactly_those_objects_in_order_with_cache_headers(stores, bucket):
    publish_first(stores["v1"], bucket)
    update = plan_update(plan_for(stores["v2"], bucket), bucket)
    wanted = {o.key for o in update.todo}

    result = publish_update(
        update, bucket, workers=4, check_store=ok_checks, fetch_root=fetch_times(6)
    )

    order = [key.removeprefix(PREFIX) for key in bucket.puts]
    assert set(order) == wanted and len(order) == len(wanted)
    phase = {o.key: o.phase for o in update.todo}
    assert [phase[k] for k in order] == sorted(phase[k] for k in order)
    assert order[-1] == "zarr.json"
    assert order[0].startswith(("0/", "1/", "2/", "3/")) and "/data/c/" in order[0]
    for key in order:
        cache, _ = bucket.headers[PREFIX + key]
        mutable = key.endswith("zarr.json") or "/time/" in key or key.startswith("volatility/")
        assert cache == (SHORT_TTL if mutable else IMMUTABLE), key
    assert hosted_digests(bucket) == local_digests(stores["v2"])
    assert (result.report.uploaded, result.report.skipped) == (len(wanted), update.unchanged)
    assert result.link is not None


def test_the_link_is_the_one_a_first_publish_gave(stores, tmp_path):
    bucket_dir = tmp_path / "bucket"
    served = bucket_dir / "aoi/store-v1"
    served.mkdir(parents=True)
    adapter = ReadingAdapter(bucket_dir)
    with serving(StoreRequestHandler, served) as url:
        first = publish(plan_for(stores["v1"], adapter, url), adapter, workers=4)
        update = plan_update(plan_for(stores["v2"], adapter, url), adapter)
        second = publish_update(update, adapter, workers=4)
    assert first.link == f"{VIEWER_URL}?store={urllib.parse.quote(url, safe='')}"
    assert second.link == first.link
    assert [c for c in second.checks if c.status == "fail"] == []
    length = next(c for c in second.checks if c.name == "hosted time length")
    assert length.status == "ok" and "6 timesteps" in length.detail
    assert hosted_digests(adapter) == local_digests(stores["v2"])


def test_an_update_that_is_already_published_uploads_nothing_and_still_verifies(stores, bucket):
    publish_first(stores["v2"], bucket)
    update = plan_update(plan_for(stores["v2"], bucket), bucket)
    assert update.todo == ()
    assert "already up to date" in update.summary(bucket)
    result = publish_update(update, bucket, check_store=ok_checks, fetch_root=fetch_times(6))
    assert bucket.puts == []
    assert result.link is not None


# -- dry run and the CLI -----------------------------------------------------------------------


@pytest.fixture
def cli_bucket(bucket, monkeypatch) -> ReadingAdapter:
    monkeypatch.setattr(adapters_module, "open_adapter", lambda destination, **options: bucket)
    return bucket


def run(*args: str, catch: bool = False):
    return CliRunner().invoke(
        main,
        ["publish", "--destination", "s3://bucket/aoi/store-v1", "--public-url", PUBLIC, *args],
        catch_exceptions=catch,
    )


def test_dry_run_writes_nothing_and_says_what_would_change(stores, cli_bucket):
    publish_first(stores["v1"], cli_bucket)
    before = hosted_digests(cli_bucket)
    result = run(str(stores["v2"]), "--update", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "4 timesteps hosted, 6 local (2 to add)" in result.output
    assert "changed in place" in result.output
    assert "dry run: nothing was uploaded" in result.output
    assert cli_bucket.puts == []
    assert hosted_digests(cli_bucket) == before


def test_cli_update_publishes_and_prints_the_unchanged_link(stores, tmp_path, monkeypatch):
    bucket_dir = tmp_path / "bucket"
    served = bucket_dir / "store-v1"
    served.mkdir(parents=True)
    adapter = ReadingAdapter(bucket_dir)
    monkeypatch.setattr(adapters_module, "open_adapter", lambda destination, **options: adapter)
    with serving(StoreRequestHandler, served) as url:
        args = ["--destination", "s3://bucket/store-v1", "--public-url", url]
        first = CliRunner().invoke(main, ["publish", str(stores["v1"]), *args])
        second = CliRunner().invoke(main, ["publish", str(stores["v2"]), *args, "--update"])
    link = f"{VIEWER_URL}?store={urllib.parse.quote(url, safe='')}"
    assert first.exit_code == 0 and link in first.output
    assert second.exit_code == 0, second.output
    assert "viewer link is unchanged" in second.output and link in second.output
    assert "[ ok ] hosted time length" in second.output


def test_update_and_overwrite_exclude_each_other(stores, cli_bucket):
    result = run(str(stores["v2"]), "--update", "--overwrite")
    assert result.exit_code == 2
    assert "exclude each other" in result.output


def test_plain_publish_still_refuses_an_existing_prefix_and_points_at_update(stores, cli_bucket):
    publish_first(stores["v1"], cli_bucket)
    result = run(str(stores["v2"]))
    assert result.exit_code == 1
    assert "Refusing to overwrite" in result.output
    assert "--update" in result.output
    assert cli_bucket.puts == []


def test_cli_refusal_is_a_one_line_error_with_every_reason(stores, cli_bucket):
    publish_first(stores["v2"], cli_bucket)
    result = run(str(stores["v1"]), "--update")
    assert result.exit_code == 1
    assert "not an earlier state" in result.output
    assert "nothing was uploaded" in result.output
    assert cli_bucket.puts == []


# -- interruption and resume -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failing", "phase_name"),
    [
        ("0/data/c/4/0/1/1", "data chunks"),
        ("0/time/c/0", "time and volatility chunks"),
        ("0/data/zarr.json", "group and array metadata"),
        ("zarr.json", "root metadata"),
    ],
)
def test_an_interrupted_update_leaves_the_old_store_readable_and_resumes(
    stores, bucket, failing, phase_name
):
    publish_first(stores["v1"], bucket)
    old_root = (bucket.root / PREFIX / "zarr.json").read_bytes()
    update = plan_update(plan_for(stores["v2"], bucket), bucket)
    bucket.fail_keys = {PREFIX + failing}

    with pytest.raises(PublishError, match=rf"phase \d \({phase_name}\).*same command again"):
        publish_update(update, bucket, workers=1, check_store=ok_checks, fetch_root=fetch_times(6))

    if failing != "zarr.json":
        assert (bucket.root / PREFIX / "zarr.json").read_bytes() == old_root
    stored = set(bucket.puts)
    assert 0 <= len(stored) < len(update.todo)
    # Every object the old root refers to still has the bytes it had.
    untouched = {
        k: v for k, v in local_digests(stores["v1"]).items() if k in hosted_digests(bucket)
    }
    for key in set(untouched) - {o.key for o in update.changed}:
        assert hosted_digests(bucket)[key] == untouched[key]

    bucket.fail_keys = set()
    bucket.puts.clear()
    resumed = plan_update(plan_for(stores["v2"], bucket), bucket)
    assert len(resumed.todo) == len(update.todo) - len(
        [k for k in stored if k.removeprefix(PREFIX) in {o.key for o in update.new}]
    ) - len([k for k in stored if k.removeprefix(PREFIX) in {o.key for o in update.changed}])
    result = publish_update(
        resumed, bucket, workers=4, check_store=ok_checks, fetch_root=fetch_times(6)
    )
    assert not stored & set(bucket.puts), "resume must not upload a stored object again"
    assert bucket.puts[-1] == PREFIX + "zarr.json"
    assert result.link is not None
    assert hosted_digests(bucket) == local_digests(stores["v2"])


# -- refusals ----------------------------------------------------------------------------------


def refusal(store: Path, bucket: ReadingAdapter) -> str:
    with pytest.raises(PublishError) as raised:
        plan_update(plan_for(store, bucket), bucket)
    assert bucket.puts == []
    return str(raised.value)


def test_refuses_when_the_host_has_timesteps_the_local_store_lacks(stores, bucket):
    publish_first(stores["v2"], bucket)
    message = refusal(stores["v1"], bucket)
    assert "6 timesteps and the local store 4" in message
    assert "not in the local store" in message


def test_refuses_a_hosted_store_on_another_grid(stores, truth, tmp_path, bucket):
    narrow = encode_head(truth[:, :, :, : WIDTH - 20], tmp_path / "narrow", 4)
    publish_first(narrow, bucket)
    message = refusal(stores["v2"], bucket)
    assert "another grid" in message


def test_refuses_a_hosted_store_with_other_times(stores, truth, tmp_path, bucket):
    shifted = make_da(truth[:4], BANDS).assign_coords(
        time=make_da(truth[:4], BANDS).time.values + np.timedelta64(1, "D")
    )
    other = tmp_path / "shifted"
    chronozarr.encode(shifted, other, chunk_size=CHUNK, volatility=True)
    publish_first(other, bucket)
    assert "timestep 0 is 2024-01-02" in refusal(stores["v2"], bucket)


def test_refuses_a_re_encode_whose_chunks_differ(stores, truth, tmp_path, bucket):
    publish_first(stores["v1"], bucket)
    other = encode_head(make_truth(N_TIME, 2, HEIGHT, WIDTH, seed=99), tmp_path / "again", 6)
    message = refusal(other, bucket)
    assert "differ from the local ones" in message
    assert "cached for a year" in message


def test_refuses_an_empty_prefix_and_names_the_plain_command(stores, bucket):
    message = refusal(stores["v2"], bucket)
    assert "is empty" in message
    assert "without --update" in message


def test_refuses_an_interrupted_first_upload(stores, bucket):
    bucket.fail_keys = {PREFIX + "zarr.json"}
    with pytest.raises(PublishError, match="root metadata"):
        upload(plan_for(stores["v1"], bucket), bucket, workers=2)
    bucket.puts.clear()
    message = refusal(stores["v2"], bucket)
    assert "no root zarr.json" in message and "interrupted first upload" in message


def test_refuses_an_adapter_that_cannot_read_objects_back(stores, tmp_path):
    plain = FakeAdapter(tmp_path / "plain")
    assert not isinstance(plain, ReadableStorage)
    with pytest.raises(PublishError, match="--update is not available for Fake storage"):
        plan_update(plan_for(stores["v2"], plain), plain)


def test_refuses_a_hosted_root_that_is_not_a_chronozarr_store(stores, bucket):
    (bucket.root / PREFIX).mkdir(parents=True)
    (bucket.root / PREFIX / "zarr.json").write_text(json.dumps({"attributes": {}}))
    assert "not a chronozarr" in refusal(stores["v2"], bucket)


# -- sharded stores ----------------------------------------------------------------------------


def test_a_sharded_append_that_rewrites_the_trailing_shard_is_refused(truth, tmp_path, bucket):
    v1 = encode_head(truth, tmp_path / "s1", 6, shard=True, shard_time=4)
    v2 = appended(v1, tmp_path / "s2", truth, 6, 7)  # shard 1 holds t=4..5 and gains t=6
    publish_first(v1, bucket)
    message = refusal(v2, bucket)
    assert "chunk or shard object(s) differ" in message
    assert "trailing shard" in message


def test_a_sharded_append_that_opens_a_new_shard_is_published(truth, tmp_path, bucket):
    v1 = encode_head(truth, tmp_path / "s1", 4, shard=True, shard_time=4)
    v2 = appended(v1, tmp_path / "s2", truth, 4, 6)  # shard 1 is new, shard 0 is not touched
    publish_first(v1, bucket)
    update = plan_update(plan_for(v2, bucket), bucket)
    assert all(o.cache_control == IMMUTABLE for o in update.new if "/data/c/" in o.key)
    publish_update(update, bucket, check_store=ok_checks, fetch_root=fetch_times(6))
    assert hosted_digests(bucket) == local_digests(v2)


# -- verification ------------------------------------------------------------------------------


def test_a_cache_still_serving_the_old_root_gives_no_link_and_a_rerun_hint(stores, bucket):
    publish_first(stores["v1"], bucket)
    update = plan_update(plan_for(stores["v2"], bucket), bucket)
    result = publish_update(
        update, bucket, workers=4, check_store=ok_checks, fetch_root=fetch_times(4)
    )
    assert result.link is None
    length = next(c for c in result.checks if c.name == "hosted time length")
    assert length.status == "fail"
    assert "lists 4 timesteps, the update has 6" in length.detail
    assert "run the same command again" in length.hint
    assert hosted_digests(bucket) == local_digests(stores["v2"])


def test_a_long_lived_root_header_warns_and_does_not_block_the_link():
    checks = hosted_root_checks(PUBLIC, 6, fetch_times(6, IMMUTABLE))
    control = next(c for c in checks if c.name == "root cache-control")
    assert control.status == "warn" and "max-age=300" in control.hint
    assert next(c for c in checks if c.name == "hosted time length").status == "ok"


def test_an_unreadable_public_root_fails_the_length_check():
    def broken(url: str) -> HostedRoot:
        raise PublishError("GET failed")

    [check] = hosted_root_checks(PUBLIC, 6, broken)
    assert check.status == "fail" and "GET failed" in check.detail


# -- the S3 adapter's read ---------------------------------------------------------------------


def test_s3_read_object_returns_the_bytes_or_none():
    from io import BytesIO

    from botocore.response import StreamingBody

    adapter = S3Adapter("bucket", region="eu-west-1")
    assert isinstance(adapter, ReadableStorage)
    with Stubber(adapter.client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(b"{}"), 2)},
            {"Bucket": "bucket", "Key": "p/zarr.json"},
        )
        assert adapter.read_object("p/zarr.json") == b"{}"
        stubber.add_client_error("get_object", "NoSuchKey", http_status_code=404)
        assert adapter.read_object("p/missing") is None
        stubber.add_client_error("get_object", "AccessDenied", http_status_code=403)
        with pytest.raises(PublishError, match="s3:GetObject"):
            adapter.read_object("p/zarr.json")


def test_xarray_still_opens_the_updated_hosted_copy(stores, bucket):
    publish_first(stores["v1"], bucket)
    update = plan_update(plan_for(stores["v2"], bucket), bucket)
    publish_update(update, bucket, check_store=ok_checks, fetch_root=fetch_times(6))
    data = chronozarr.open_store(bucket.root / PREFIX.rstrip("/")).to_xarray(lod=0)
    assert isinstance(data, xr.DataArray) and data.sizes["time"] == 6
