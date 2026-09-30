"""`convert`: COG manifests, Zarr and NetCDF sources, resampling, resume, estimates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr

import chronozarr
from chronozarr.convert import CogManifestSource, convert, plan_conversion, read_manifest
from tests.synthetic import CRS, TRANSFORM, make_times, make_truth

pytestmark = pytest.mark.unit

rasterio = pytest.importorskip("rasterio")
from rasterio.transform import Affine  # noqa: E402  (after importorskip)

N_TIME, N_BAND, HEIGHT, WIDTH = 5, 2, 40, 50
OPTIONS: dict[str, Any] = {"chunk_size": 16, "anchor_interval": 2}
DATES = ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01", "2024-05-01"]


def write_tif(
    path: Path,
    array: np.ndarray,
    *,
    transform=TRANSFORM,
    crs: str | None = CRS,
    nodata: float | None = 0,
    descriptions: list[str] | None = None,
) -> Path:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[1],
        width=array.shape[2],
        count=array.shape[0],
        dtype=array.dtype,
        crs=crs,
        transform=Affine(*transform),
        nodata=nodata,
    ) as dst:
        dst.write(array)
        if descriptions:
            dst.descriptions = descriptions
    return path


@pytest.fixture
def truth() -> np.ndarray:
    return make_truth(N_TIME, N_BAND, HEIGHT, WIDTH)


@pytest.fixture
def tifs(tmp_path, truth) -> list[Path]:
    folder = tmp_path / "cogs"
    folder.mkdir()
    return [write_tif(folder / f"scene_{t}.tif", truth[t]) for t in range(N_TIME)]


def write_csv(path: Path, tifs: list[Path], dates=DATES, bands: str | None = None) -> Path:
    header = "uri,datetime" + (",bands" if bands else "")
    rows = [
        f"{tif},{date}" + (f",{bands}" if bands else "")
        for tif, date in zip(tifs, dates, strict=True)
    ]
    path.write_text("\n".join([header, *rows[::-1]]) + "\n")  # reversed: the manifest is unsorted
    return path


def stored(store_path: Path) -> np.ndarray:
    return chronozarr.open_store(store_path).to_xarray().values


# --- manifest parsing -----------------------------------------------------------------------


def test_read_manifest_sorts_resolves_relative_uris_and_converts_offsets(tmp_path):
    manifest = tmp_path / "m.csv"
    manifest.write_text(
        "uri,datetime,bands\n"
        "b.tif,2024-03-01T02:00:00+02:00,red;nir\n"
        "a.tif,2024-01-15,red;nir\n"
        "https://example.org/c.tif,2024-02-01T00:00:00Z,\n"
    )
    entries, bands = read_manifest(manifest)
    assert [str(e.time) for e in entries] == [
        "2024-01-15T00:00:00.000",
        "2024-02-01T00:00:00.000",
        "2024-03-01T00:00:00.000",
    ]
    assert entries[0].uri == str(tmp_path / "a.tif")
    assert entries[1].uri == "https://example.org/c.tif"
    assert bands == ("red", "nir")


def test_read_manifest_json_forms(tmp_path):
    items = [
        {"uri": "a.tif", "datetime": "2024-01-01"},
        {"uri": "b.tif", "datetime": "2024-02-01"},
    ]
    as_list = tmp_path / "list.json"
    as_list.write_text(json.dumps(items))
    as_object = tmp_path / "object.json"
    as_object.write_text(json.dumps({"bands": ["r", "g"], "items": items}))
    assert read_manifest(as_list)[1] is None
    assert read_manifest(as_object)[1] == ("r", "g")


@pytest.mark.parametrize(
    ("content", "suffix", "message"),
    [
        ("uri,when\na.tif,2024-01-01\n", ".csv", "missing \\['datetime'\\]"),
        ("uri,datetime\na.tif,yesterday\n", ".csv", "cannot parse datetime 'yesterday'"),
        ("uri,datetime\na.tif,2024-01-01\nb.tif,2024-01-01\n", ".csv", "share the datetime"),
        ("uri,datetime,bands\na,2024-01-01,r;g\nb,2024-02-01,r;n\n", ".csv", "differ from"),
        ("uri,datetime\n", ".csv", "no rows"),
        ('[{"uri": "a.tif"}]', ".json", "needs both 'uri' and 'datetime'"),
        ("x", ".txt", "must be .csv or .json"),
    ],
)
def test_read_manifest_rejects_bad_manifests(tmp_path, content, suffix, message):
    path = tmp_path / f"m{suffix}"
    path.write_text(content)
    with pytest.raises(ValueError, match=message):
        read_manifest(path)


# --- COG manifests --------------------------------------------------------------------------


def test_manifest_roundtrip_is_bit_exact(tmp_path, tifs, truth):
    manifest = write_csv(tmp_path / "m.csv", tifs, bands="B04;B08")
    out = tmp_path / "store"
    report = convert(manifest, out, encoding="star-delta", **OPTIONS)

    assert np.array_equal(stored(out), truth)
    store = chronozarr.open_store(out)
    assert store.bands == ("B04", "B08")
    assert store.attrs.times[0] == "2024-01-01T00:00:00Z"
    assert store.attrs.crs == CRS
    assert store.levels[0].transform == TRANSFORM
    assert store.attrs.nodata == 0
    assert chronozarr.validate(out) == []
    assert (report.n_staged, report.n_reused) == (N_TIME, 0)
    assert report.encode is not None
    assert report.encode.encoding == "star-delta"
    assert report.total_s == report.read_s + report.encode_s
    assert not (tmp_path / "store.convert-work").exists()  # removed on success


def test_band_names_fall_back_to_descriptions_then_indexes(tmp_path, truth):
    described = tmp_path / "described"
    described.mkdir()
    files = [
        write_tif(described / f"{t}.tif", truth[t], descriptions=["red", "nir"]) for t in range(3)
    ]
    out = tmp_path / "a"
    convert(write_csv(tmp_path / "a.csv", files, DATES[:3]), out, **OPTIONS)
    assert chronozarr.open_store(out).bands == ("red", "nir")

    plain = tmp_path / "plain"
    plain.mkdir()
    files = [write_tif(plain / f"{t}.tif", truth[t]) for t in range(3)]
    out = tmp_path / "b"
    convert(write_csv(tmp_path / "b.csv", files, DATES[:3]), out, **OPTIONS)
    assert chronozarr.open_store(out).bands == ("1", "2")


def test_int16_sources_keep_dtype_and_their_own_nodata(tmp_path):
    rng = np.random.default_rng(3)
    data = rng.integers(-2000, 2000, size=(3, 1, 24, 30)).astype(np.int16)
    data[:, :, :4, :4] = -9999
    folder = tmp_path / "i16"
    folder.mkdir()
    files = [write_tif(folder / f"{t}.tif", data[t], nodata=-9999) for t in range(3)]
    out = tmp_path / "store"
    convert(write_csv(tmp_path / "m.csv", files, DATES[:3]), out, **OPTIONS)
    store = chronozarr.open_store(out)
    assert store.attrs.nodata == -9999
    assert store.attrs.temporal.encoding == "none"  # star-delta is uint8/uint16 only
    assert np.array_equal(store.to_xarray().values, data)
    assert store.levels[0].data.dtype == np.int16


def test_source_nodata_can_be_overridden_or_removed(tmp_path, tifs):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    out = tmp_path / "none"
    convert(manifest, out, nodata=None, **OPTIONS)
    assert chronozarr.open_store(out).attrs.nodata is None
    out = tmp_path / "seven"
    convert(manifest, out, nodata=7, **OPTIONS)
    assert chronozarr.open_store(out).attrs.nodata == 7


def test_grid_mismatch_needs_explicit_resampling_and_then_warps(tmp_path, tifs, truth):
    shifted = list(TRANSFORM)
    shifted[2] += 5 * 10.0  # five pixels east: same size, different extent
    write_tif(tifs[2], truth[2], transform=tuple(shifted))
    manifest = write_csv(tmp_path / "m.csv", tifs)

    with pytest.raises(ValueError, match=r"1 of 5 sources are not on the target grid") as error:
        convert(manifest, tmp_path / "refused", **OPTIONS)
    assert tifs[2].name in str(error.value)
    assert "--resampling" in str(error.value)
    assert "transform" in str(error.value)
    assert not (tmp_path / "refused").exists()

    out = tmp_path / "warped"
    report = convert(manifest, out, resampling="nearest", encoding="none", **OPTIONS)
    assert report.plan.warped == 1
    result = stored(out)
    for t in (0, 1, 3, 4):
        assert np.array_equal(result[t], truth[t])
    # source column j of the shifted scene lands at target column j + 5; the rest is nodata
    assert np.array_equal(result[2][:, :, 5:], truth[2][:, :, :-5])
    assert not result[2][:, :, :5].any()


def test_crs_override_warps_every_timestep_into_the_new_crs(tmp_path, tifs):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    out = tmp_path / "store"
    report = convert(manifest, out, crs="EPSG:32632", resampling="bilinear", **OPTIONS)
    assert report.plan.warped == N_TIME
    store = chronozarr.open_store(out)
    assert store.attrs.crs == "EPSG:32632"
    assert store.read(0).any()
    grid = report.plan.source.info.grid
    assert (grid.height, grid.width) == store.levels[0].shape[2:]
    assert chronozarr.validate(out) == []


def test_explicit_transform_and_shape_crop_the_grid(tmp_path, tifs, truth):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    left = TRANSFORM[2] + 4 * 10.0
    top = TRANSFORM[5] - 3 * 10.0
    out = tmp_path / "store"
    convert(
        manifest,
        out,
        crs=CRS,
        transform=(10.0, 0.0, left, 0.0, -10.0, top),
        shape=(20, 30),
        resampling="nearest",
        encoding="none",
        **OPTIONS,
    )
    assert np.array_equal(stored(out), truth[:, :, 3:23, 4:34])


def test_grid_override_options_are_validated(tmp_path, tifs):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    with pytest.raises(ValueError, match="needs --shape and --crs"):
        plan_conversion(manifest, transform=TRANSFORM, sample=False)
    with pytest.raises(ValueError, match="only meaningful together with --transform"):
        plan_conversion(manifest, shape=(10, 10), sample=False)
    with pytest.raises(ValueError, match="unknown resampling"):
        plan_conversion(manifest, crs="EPSG:32632", resampling="magic", sample=False)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        ("dtype", "All sources must share one dtype"),
        ("bands", "has 1 bands"),
        ("nodata", "Pass --nodata"),
        ("int32", "chronozarr stores hold"),
        ("nocrs", "cannot open source"),
    ],
)
def test_inconsistent_or_unsupported_sources_are_rejected_up_front(
    tmp_path, tifs, truth, mutate, message
):
    bad = tifs[3]
    if mutate == "dtype":
        write_tif(bad, truth[3].astype(np.uint8))
    elif mutate == "bands":
        write_tif(bad, truth[3][:1])
    elif mutate == "nodata":
        write_tif(bad, truth[3], nodata=7)
    elif mutate == "int32":
        tifs = [write_tif(t, truth[i].astype(np.int32)) for i, t in enumerate(tifs)]
    else:
        write_tif(bad, truth[3], crs=None)
    with pytest.raises(ValueError, match=message):
        plan_conversion(write_csv(tmp_path / "m.csv", tifs), sample=False)


def test_missing_source_file_is_named(tmp_path, tifs):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    tifs[1].rename(tmp_path / "moved.tif")
    with pytest.raises(ValueError, match=f"cannot open source .*{tifs[1].name}"):
        plan_conversion(manifest, sample=False)


# --- Zarr and NetCDF sources ----------------------------------------------------------------


def dataset_from(truth: np.ndarray, *, band: bool = True, flip_y: bool = False) -> xr.Dataset:
    a, _, c, _, e, f = TRANSFORM
    x = c + a * (np.arange(WIDTH) + 0.5)
    y = f + e * (np.arange(HEIGHT) + 0.5)
    values = truth if band else truth[:, 0]
    dims = ("time", "band", "y", "x") if band else ("time", "y", "x")
    coords: dict = {"time": make_times(len(truth)), "y": y, "x": x}
    if band:
        coords["band"] = ["red", "nir"]
    da = xr.DataArray(values, dims=dims, coords=coords, name="reflectance")
    if flip_y:
        da = da.isel(y=slice(None, None, -1))
    return da.to_dataset()


def test_zarr_source_with_band_dimension(tmp_path, truth):
    source = tmp_path / "in.zarr"
    ds = dataset_from(truth)
    ds["reflectance"].attrs.update({"crs": CRS, "scale_factor": 0.0001, "units": "1"})
    ds.to_zarr(source, zarr_format=2, consolidated=True)
    out = tmp_path / "store"
    report = convert(source, out, encoding="star-delta", **OPTIONS)
    assert np.array_equal(stored(out), truth)
    store = chronozarr.open_store(out)
    assert store.bands == ("red", "nir")
    assert store.attrs.bands[0].scale == 0.0001
    assert store.attrs.bands[0].units == "1"
    assert store.levels[0].transform == TRANSFORM
    assert report.plan.source.kind == "Zarr store"


def test_zarr_source_without_band_dimension_flipped_y_and_crs_flag(tmp_path, truth):
    source = tmp_path / "in.zarr"
    dataset_from(truth, band=False, flip_y=True).to_zarr(source, zarr_format=2, consolidated=False)
    with pytest.raises(ValueError, match="does not declare a CRS"):
        plan_conversion(source, sample=False)
    out = tmp_path / "store"
    convert(source, out, crs=CRS, encoding="none", **OPTIONS)
    store = chronozarr.open_store(out)
    assert store.bands == ("reflectance",)
    assert np.array_equal(store.to_xarray().values[:, 0], truth[:, 0])
    assert store.levels[0].transform == TRANSFORM


def test_zarr_source_dimension_names_and_errors(tmp_path, truth):
    source = tmp_path / "in.zarr"
    ds = dataset_from(truth).rename({"x": "lon", "y": "lat", "band": "channel"})
    ds.attrs["crs"] = CRS
    ds.to_zarr(source, zarr_format=2, consolidated=True)
    out = tmp_path / "store"
    convert(source, out, encoding="none", **OPTIONS)  # lat/lon/channel are recognised aliases
    assert np.array_equal(stored(out), truth)

    odd = tmp_path / "odd.zarr"
    dataset_from(truth).rename({"x": "col"}).to_zarr(odd, zarr_format=2, consolidated=True)
    with pytest.raises(ValueError, match="cannot find the x dimension"):
        plan_conversion(odd, crs=CRS, sample=False)
    plan = plan_conversion(odd, crs=CRS, dims="x=col", sample=False)
    assert plan.source.info.grid.width == WIDTH
    with pytest.raises(ValueError, match="bad --dims entry"):
        plan_conversion(odd, crs=CRS, dims="x", sample=False)
    with pytest.raises(ValueError, match="choose one with --variable"):
        two = dataset_from(truth).assign(other=lambda d: d["reflectance"])
        two_path = tmp_path / "two.zarr"
        two.to_zarr(two_path, zarr_format=2, consolidated=True)
        plan_conversion(two_path, crs=CRS, sample=False)
    with pytest.raises(ValueError, match="apply to COG manifests"):
        plan_conversion(source, resampling="nearest", sample=False)


def test_netcdf_source(tmp_path, truth):
    pytest.importorskip("h5netcdf")
    source = tmp_path / "in.nc"
    ds = dataset_from(truth)
    ds["reflectance"].attrs["crs"] = CRS
    ds.to_netcdf(source, engine="h5netcdf")
    out = tmp_path / "store"
    report = convert(source, out, encoding="none", **OPTIONS)
    assert report.plan.source.kind == "NetCDF file"
    assert np.array_equal(stored(out), truth)


def test_missing_xarray_sources_are_named(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        plan_conversion(tmp_path / "nope.zarr", sample=False)
    with pytest.raises(FileNotFoundError, match=r"NetCDF file .* does not exist"):
        plan_conversion(tmp_path / "nope.nc", sample=False)


# --- estimates, dry run, resume -------------------------------------------------------------


def test_dry_run_reports_estimates_and_writes_nothing(tmp_path, tifs):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    out = tmp_path / "store"
    seen = []
    report = convert(manifest, out, dry_run=True, on_plan=seen.append, **OPTIONS)
    assert report.encode is None
    assert not out.exists()
    assert not (tmp_path / "store.convert-work").exists()
    plan = seen[0]
    assert plan.raw_bytes == N_TIME * N_BAND * HEIGHT * WIDTH * 2
    assert plan.timestep_bytes == N_BAND * HEIGHT * WIDTH * 2
    assert plan.est_output_bytes is not None and plan.sample_ratio is not None
    assert 0 < plan.sample_ratio < 1.0
    text = "\n".join(plan.lines())
    for expected in ("source:     manifest of COGs, 5 timesteps", "grid:", "raw size:", "output:"):
        assert expected in text
    assert "resampling: none needed" in text
    assert "time:       read about" in text

    actual = convert(manifest, out, **OPTIONS).encode
    assert actual is not None
    assert 0.25 < plan.est_output_bytes / actual.total_bytes < 4.0


def test_progress_reports_every_staged_timestep(tmp_path, tifs):
    calls = []
    convert(
        write_csv(tmp_path / "m.csv", tifs),
        tmp_path / "store",
        progress=lambda done, total: calls.append((done, total)),
        **OPTIONS,
    )
    assert calls[0] == (0, N_TIME)
    assert calls[-1] == (N_TIME, N_TIME)
    assert [done for done, _ in calls] == sorted(done for done, _ in calls)


def test_resume_reads_only_what_the_interrupted_run_did_not_stage(
    tmp_path, tifs, truth, monkeypatch
):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    out = tmp_path / "store"
    original = CogManifestSource.read
    state = {"fail": True}

    def flaky(self, t):
        if t == 3 and state["fail"]:
            raise OSError("connection reset")
        return original(self, t)

    monkeypatch.setattr(CogManifestSource, "read", flaky)
    with pytest.raises(OSError, match="connection reset") as error:
        convert(manifest, out, read_ahead=1, **OPTIONS)
    assert "rerun with --resume" in "\n".join(error.value.__notes__)
    work = tmp_path / "store.convert-work"
    assert sorted(p.name for p in work.glob("t*.npy")) == [
        "t000000.npy",
        "t000001.npy",
        "t000002.npy",
    ]
    assert not out.exists()

    with pytest.raises(FileExistsError, match="Pass --resume"):
        convert(manifest, out, **OPTIONS)

    state["fail"] = False
    report = convert(manifest, out, resume=True, **OPTIONS)
    assert (report.n_reused, report.n_staged) == (3, 2)
    assert np.array_equal(stored(out), truth)
    assert not work.exists()


def test_resume_refuses_a_work_dir_staged_for_a_different_plan(tmp_path, tifs, truth, monkeypatch):
    manifest = write_csv(tmp_path / "m.csv", tifs)
    out = tmp_path / "store"
    original = CogManifestSource.read

    def failing(self, t):
        if t == 1:  # timesteps 0, 2 and 4 are read while planning, so the failure comes later
            raise OSError("boom")
        return original(self, t)

    monkeypatch.setattr(CogManifestSource, "read", failing)
    with pytest.raises(OSError, match="boom"):
        convert(manifest, out, read_ahead=1, **OPTIONS)
    monkeypatch.undo()
    other = write_csv(tmp_path / "other.csv", tifs, bands="X;Y")
    with pytest.raises(ValueError, match="different input or options"):
        convert(other, out, resume=True, **OPTIONS)


def test_existing_output_is_refused_before_anything_is_read(tmp_path, tifs):
    out = tmp_path / "store"
    out.mkdir()
    (out / "file").write_text("x")
    with pytest.raises(FileExistsError, match="stores are immutable"):
        convert(write_csv(tmp_path / "m.csv", tifs), out, **OPTIONS)
    assert not (tmp_path / "store.convert-work").exists()


def test_custom_work_dir_is_used_and_cleaned(tmp_path, tifs):
    work = tmp_path / "scratch" / "stage"
    convert(write_csv(tmp_path / "m.csv", tifs), tmp_path / "store", work_dir=work, **OPTIONS)
    assert not work.exists()


def test_plan_lines_mention_warped_timesteps(tmp_path, tifs, truth):
    shifted = list(TRANSFORM)
    shifted[2] += 10.0
    write_tif(tifs[1], truth[1], transform=tuple(shifted))
    plan = plan_conversion(write_csv(tmp_path / "m.csv", tifs), resampling="average")
    assert "resampling: 1 of 5 timesteps are warped" in "\n".join(plan.lines())
