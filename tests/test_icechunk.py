"""Pinned Icechunk imports use ordinary conversion, without an intermediate raster store."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

from chronozarr import IcechunkInput, open_store
from chronozarr.cli import main
from chronozarr.convert import XarraySource, _check_work_dir, convert, plan_conversion
from chronozarr.icechunk import parse_indexers

pytestmark = pytest.mark.unit


def test_selections_are_explicit_and_immutable() -> None:
    indexes = {"time": slice(1, 3), "y": slice(0, 4), "x": slice(0, 4)}
    source = IcechunkInput("repo", indexes)
    indexes["time"] = slice(5, 6)
    assert source.indexers["time"] == slice(1, 3)
    with pytest.raises(TypeError):
        cast("Any", source.indexers)["time"] = 0
    for value in (-1, True, slice(None, 2), slice(1, 1), slice(0, 3, 2)):
        with pytest.raises(ValueError):
            IcechunkInput("repo", {"time": value})
    assert parse_indexers(("time=0:2", "member=1")) == {"time": slice(0, 2), "member": 1}
    for entries in (("time=0:2:3",), ("time=a",), ("x=1", "x=2")):
        with pytest.raises(ValueError):
            parse_indexers(entries)


def test_versions_and_anonymous_access_are_explicit() -> None:
    with pytest.raises(ValueError, match="choose one"):
        IcechunkInput("repo", {}, branch="main", snapshot="abc")
    for repository in (
        "http://example.com/repo",
        "s3://bucket/repo",
        "https://example.com/repo?token=x",
    ):
        with pytest.raises(ValueError):
            IcechunkInput(repository, {})
    for prefix in ("s3://bucket", "file:///tmp/", "https://user:pass@example.com/"):
        with pytest.raises(ValueError):
            IcechunkInput("repo", {}, virtual_prefixes=(prefix,))


def _dataset(dtype: str = "uint16", *, forecast: bool = False) -> xr.Dataset:
    values = np.arange(36).reshape(3, 3, 4).astype(dtype)
    values[0, 1, 1] = 0
    coord = {
        "time": np.array(["2024-01-01", "2024-01-02", "2024-01-03"], dtype="datetime64[ns]"),
        "y": [300.0, 290.0, 280.0],
        "x": [100.0, 110.0, 120.0, 130.0],
    }
    ds = xr.Dataset({"temp": (("time", "y", "x"), values)}, coords=coord)
    ds["temp"].attrs = {"units": "K", "scale_factor": 0.1, "add_offset": 250.0}
    ds.attrs["crs"] = "EPSG:32618"
    ds["valid"] = (("time", "y", "x"), np.ones(values.shape, dtype=np.uint8))
    ds["valid"].values[0, 0, 0] = 0
    if forecast:
        ds = (
            ds.rename({"time": "lead_time"})
            .assign_coords(lead_time=np.array([0, 1, 2], dtype="timedelta64[h]"))
            .expand_dims(init_time=np.array(["2024-01-01"], dtype="datetime64[ns]"))
        )
    return ds


def _repo(tmp_path: Path, dataset: xr.Dataset, *, sharded: bool = False):
    icechunk = pytest.importorskip("icechunk")
    path = tmp_path / "input.icechunk"
    repo = icechunk.Repository.create(
        icechunk.local_filesystem_storage(str(path)),
        config=icechunk.RepositoryConfig(inline_chunk_threshold_bytes=0),
    )
    session = repo.writable_session("main")
    dataset.to_zarr(
        session.store,
        zarr_format=3,
        consolidated=False,
        encoding={"temp": {"chunks": (3, 1, 2), "shards": (3, 3, 4)}} if sharded else None,
    )
    snapshot = session.commit("first")
    return path, repo, snapshot


def _indexes() -> dict[str, int | slice]:
    return {"time": slice(0, 2), "y": slice(0, 3), "x": slice(0, 4)}


def test_local_roundtrip_preserves_raw_values_scale_mask_and_identity(tmp_path: Path) -> None:
    original = _dataset()
    path, _, snapshot = _repo(tmp_path, original)
    source = IcechunkInput(path, _indexes(), snapshot=snapshot)
    out = tmp_path / "out"
    report = convert(
        source, out, variable="temp", mask_var="valid", nodata=None, chunk_size=16, n_lods=1
    )
    opened = open_store(out)
    np.testing.assert_array_equal(opened.to_xarray().values[:, 0], original.temp.values[:2])
    band = opened.attrs.bands[0]
    assert (band.scale, band.offset, band.units) == (0.1, 250.0, "K")
    assert report.plan.source.info.mask
    mask = opened.read_mask(0)
    assert mask is not None
    np.testing.assert_array_equal(mask, original.valid.values[0])
    assert mask[1, 1] == 1  # a valid zero remains valid
    assert "snapshot:" in "\n".join(report.plan.lines())
    assert opened.attrs.provenance is not None
    notes = opened.attrs.provenance["notes"]
    identity = json.loads(notes.removeprefix("Icechunk import: "))
    assert identity["snapshot"] == snapshot
    assert identity["indexers"] == {"time": [0, 2], "y": [0, 3], "x": [0, 4]}


def test_branch_advancement_does_not_change_open_plan_or_reuse_staging(tmp_path: Path) -> None:
    original = _dataset()
    path, repo, first = _repo(tmp_path, original)
    source = IcechunkInput(path, _indexes(), branch="main")
    plan = plan_conversion(source, variable="temp", sample=False)
    work = tmp_path / "work"
    _check_work_dir(work, plan, False)
    session = repo.writable_session("main")
    import zarr

    zarr.open_array(store=session.store, path="temp", mode="r+")[:] = 99
    second = session.commit("changed values")
    assert second != first
    np.testing.assert_array_equal(plan.source.read(0).data[0], original.temp.values[0])
    advanced = plan_conversion(source, variable="temp", sample=False)
    with pytest.raises(ValueError, match="different input"):
        _check_work_dir(work, advanced, True)
    pinned = plan_conversion(
        IcechunkInput(path, _indexes(), snapshot=first), variable="temp", sample=False
    )
    assert isinstance(pinned.source, XarraySource)
    assert pinned.source.icechunk_identity is not None
    assert pinned.source.icechunk_identity["snapshot"] == first
    _check_work_dir(work, pinned, True)


def test_bounds_and_chunk_budget_fail_before_pixel_sampling(tmp_path: Path) -> None:
    path, _, _ = _repo(tmp_path, _dataset())
    cases: list[tuple[dict[str, int | slice], dict[str, Any], str]] = [
        ({"time": slice(0, 2)}, {}, "missing"),
        ({**_indexes(), "x": slice(0, 5)}, {}, "dimension has"),
        (_indexes(), {"max_output_bytes": 1}, "selected decoded output"),
        (_indexes(), {"max_chunk_bytes": 1}, "decoded source chunk"),
    ]
    for indexes, kwargs, message in cases:
        with pytest.raises(ValueError, match=message):
            plan_conversion(IcechunkInput(path, indexes, **kwargs), variable="temp", sample=False)


def test_float64_requires_explicit_rounding_and_records_it(tmp_path: Path) -> None:
    original = _dataset("float64")
    original["temp"].values[:] += 0.123456789
    path, _, _ = _repo(tmp_path, original)
    with pytest.raises(ValueError, match="float64"):
        plan_conversion(IcechunkInput(path, _indexes()), variable="temp", sample=False)
    source = IcechunkInput(path, _indexes(), cast_float32=True)
    out = tmp_path / "out"
    convert(source, out, variable="temp", nodata=None, chunk_size=16, n_lods=1)
    opened = open_store(out)
    np.testing.assert_array_equal(
        opened.to_xarray().values[:, 0], original.temp.values[:2].astype("float32")
    )
    assert opened.attrs.provenance is not None
    assert '"source_dtype":"float64"' in opened.attrs.provenance["notes"]
    assert '"output_dtype":"float32"' in opened.attrs.provenance["notes"]


def test_forecast_uses_valid_time_and_requires_one_run(tmp_path: Path) -> None:
    forecast = _dataset(forecast=True)
    path, _, _ = _repo(tmp_path, forecast)
    indexes = {"init_time": 0, "lead_time": slice(0, 3), "y": slice(0, 3), "x": slice(0, 4)}
    plan = plan_conversion(IcechunkInput(path, indexes), variable="temp", sample=False)
    np.testing.assert_array_equal(
        plan.source.times,
        np.array(["2024-01-01T00", "2024-01-01T01", "2024-01-01T02"], dtype="datetime64[h]"),
    )
    assert isinstance(plan.source, XarraySource)
    assert plan.source.icechunk_identity is not None
    assert "valid_time = init_time" in plan.source.icechunk_identity["forecast"]
    doubled = xr.concat(
        [
            forecast,
            forecast.assign_coords(init_time=np.array(["2024-01-02"], dtype="datetime64[ns]")),
        ],
        dim="init_time",
    )
    other = tmp_path / "other"
    other.mkdir()
    path, _, _ = _repo(other, doubled)
    with pytest.raises(ValueError, match="one explicit init_time"):
        plan_conversion(
            IcechunkInput(path, {**indexes, "init_time": slice(0, 2)}),
            variable="temp",
            sample=False,
        )


def test_cli_rejects_unrelated_icechunk_options() -> None:
    result = CliRunner().invoke(main, ["convert", "anything", "out", "--isel", "time=0:2"])
    assert result.exit_code != 0
    assert "require --icechunk" in result.output


def test_float32_rounding_preserves_sentinel_validity_and_refuses_overflow(tmp_path: Path) -> None:
    original = _dataset("float64")
    original["temp"].attrs["_FillValue"] = 1.000000001
    original["temp"].values[0, 0, :2] = [1.000000001, 1.000000002]
    path, repo, _ = _repo(tmp_path, original)
    source = IcechunkInput(path, _indexes(), cast_float32=True)
    plan = plan_conversion(source, variable="temp", sample=False)
    step = plan.source.read(0)
    assert step.valid is not None
    np.testing.assert_array_equal(step.valid[0, :2], [0, 1])
    np.testing.assert_array_equal(step.data[0, 0, :2], [1, 1])
    session = repo.writable_session("main")
    import zarr

    zarr.open_array(store=session.store, path="temp", mode="r+")[0, 0, 0] = 1e300
    session.commit("overflow")
    plan = plan_conversion(source, variable="temp", sample=False)
    with pytest.raises(ValueError, match="overflow float32"):
        plan.source.read(0)


def test_virtual_resume_is_rejected_before_reading(tmp_path: Path, monkeypatch) -> None:
    def unexpected(*args):
        raise AssertionError("resume rejection must happen before repository access")

    monkeypatch.setattr("chronozarr.icechunk._open_repository", unexpected)
    source = IcechunkInput(
        "https://example.com/repo", _indexes(), virtual_prefixes=("s3://public-bucket/",)
    )
    with pytest.raises(ValueError, match="resume is unsupported"):
        convert(source, tmp_path / "out", variable="temp", resume=True)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("cache_bytes", [0, 1, 32 * 1024 * 1024])
def test_native_encoded_range_cache_reuses_frames_with_explicit_budget(
    tmp_path: Path, monkeypatch, cache_bytes: int
) -> None:
    import threading
    from functools import partial
    from http.server import ThreadingHTTPServer

    from chronozarr.view import StoreRequestHandler

    icechunk = pytest.importorskip("icechunk")
    original = _dataset()
    path, _, _ = _repo(tmp_path, original, sharded=True)
    ranges: list[tuple[str, str | None]] = []

    class Metered(StoreRequestHandler):
        def do_GET(self):
            ranges.append((self.path, self.headers.get("Range")))
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Metered, root=path, prefix=path.name))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_http_storage = icechunk.http_storage
    loopback = f"http://127.0.0.1:{server.server_address[1]}/{path.name}"
    monkeypatch.setattr(icechunk, "http_storage", lambda url: original_http_storage(loopback))
    try:
        plan = plan_conversion(
            IcechunkInput(
                "https://fixture.invalid/repo", _indexes(), encoded_cache_bytes=cache_bytes
            ),
            variable="temp",
            mask_var="valid",
            sample=False,
        )
        first = len(ranges)
        step = plan.source.read(0)
        np.testing.assert_array_equal(step.data[0], original.temp.values[0])
        np.testing.assert_array_equal(step.valid, original.valid.values[0])
        first_reads = ranges[first:]
        assert first_reads
        assert any("/chunks/" in key and request_range for key, request_range in first_reads)
        assert len({request_range for _, request_range in first_reads}) > 1
        first = len(ranges)
        np.testing.assert_array_equal(plan.source.read(1).data[0], original.temp.values[1])
        np.testing.assert_array_equal(plan.source.read(0).data[0], original.temp.values[0])
        if cache_bytes > 1:
            assert len(ranges) == first, "same logical time chunk must reuse encoded ranges"
        else:
            assert len(ranges) > first, "disabled/tiny cache cannot retain the encoded ranges"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
