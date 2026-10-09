"""Command line: encode from Zarr and GeoTIFF inputs, validate, info."""

from __future__ import annotations

import sys

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr import schema
from chronozarr.cli import main
from tests.synthetic import CRS, TRANSFORM, make_da, make_truth

pytestmark = pytest.mark.unit


def _run(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


@pytest.fixture
def zarr_input(tmp_path):
    truth = make_truth(4, 2, 40, 50)
    path = tmp_path / "input.zarr"
    make_da(truth).to_dataset(name="reflectance").to_zarr(path, zarr_format=2, consolidated=False)
    return path, truth


def test_encode_validate_info_from_zarr(tmp_path, zarr_input):
    source, truth = zarr_input
    out = tmp_path / "out"
    result = _run(
        "encode",
        str(source),
        str(out),
        "--chunk-size",
        "16",
    )
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert np.array_equal(chronozarr.open_store(out).to_xarray().values, truth)

    ok = _run("validate", str(out))
    assert ok.exit_code == 0
    assert f"conforms to chronozarr {schema.SPEC_VERSION}" in ok.output

    info = _run("info", str(out))
    assert info.exit_code == 0
    for expected in (
        f"chronozarr {schema.SPEC_VERSION}",
        "EPSG:32631",
        "times:     4",
        "grid 3x4",
    ):
        assert expected in info.output


def test_encode_is_unsharded_unless_shard_is_given(tmp_path, zarr_input):
    source, _ = zarr_input
    default, sharded = tmp_path / "default", tmp_path / "sharded"
    assert _run("encode", str(source), str(default), "--chunk-size", "16").exit_code == 0
    assert (
        _run("encode", str(source), str(sharded), "--chunk-size", "16", "--shard").exit_code == 0
    )
    assert chronozarr.open_store(default).levels[0].data.shards is None
    assert chronozarr.open_store(sharded).levels[0].data.shards is not None


def test_shard_time_without_shard_is_a_click_error(tmp_path, zarr_input):
    source, _ = zarr_input
    result = CliRunner().invoke(
        main, ["encode", str(source), str(tmp_path / "out"), "--shard-time", "2"]
    )
    assert result.exit_code == 2
    assert "--shard-time needs --shard" in result.output
    assert not (tmp_path / "out").exists()


def test_no_shard_and_lods_options(tmp_path, zarr_input):
    source, _ = zarr_input
    out = tmp_path / "out"
    result = _run(
        "encode", str(source), str(out), "--chunk-size", "16", "--no-shard", "--lods", "2"
    )
    assert result.exit_code == 0, result.output
    store = chronozarr.open_store(out)
    assert len(store.levels) == 2
    assert store.levels[0].data.shards is None


def test_encode_reports_errors_as_click_errors(tmp_path, zarr_input):
    source, _ = zarr_input
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "file").write_text("x")
    result = CliRunner().invoke(main, ["encode", str(source), str(tmp_path / "taken")])
    assert result.exit_code == 1
    assert "not empty" in result.output

    missing = CliRunner().invoke(
        main, ["encode", str(tmp_path / "nope.zarr"), str(tmp_path / "o")]
    )
    assert missing.exit_code == 1
    assert "does not exist" in missing.output


def test_validate_fails_with_problem_list(tmp_path):
    (tmp_path / "empty").mkdir()
    result = CliRunner().invoke(main, ["validate", str(tmp_path / "empty")])
    assert result.exit_code == 1
    assert "no Zarr v3 group found" in result.output


def test_encode_from_geotiff_glob(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import Affine

    truth = make_truth(3, 2, 20, 30)
    tif_dir = tmp_path / "tifs"
    tif_dir.mkdir()
    transform = Affine(10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0)
    for t, name in enumerate(["S2_20240315", "S2_20240115", "S2_20240215"]):
        with rasterio.open(
            tif_dir / f"{name}.tif",
            "w",
            driver="GTiff",
            height=20,
            width=30,
            count=2,
            dtype="uint16",
            crs="EPSG:32631",
            transform=transform,
        ) as dst:
            dst.write(truth[t])
    out = tmp_path / "out"
    result = _run("encode", str(tif_dir / "*.tif"), str(out), "--chunk-size", "16")
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(out)
    assert store.attrs.times == (
        "2024-01-15T00:00:00Z",
        "2024-02-15T00:00:00Z",
        "2024-03-15T00:00:00Z",
    )
    assert store.attrs.crs == "EPSG:32631"
    assert store.levels[0].transform == (10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0)
    expected = truth[[1, 2, 0]]  # file order was 03-15, 01-15, 02-15; store is time-sorted
    assert np.array_equal(store.to_xarray().values, expected)
    assert store.bands == ("1", "2")


def test_geotiff_without_date_in_name_fails(tmp_path):
    pytest.importorskip("rasterio")
    (tmp_path / "scene.tif").write_bytes(b"")
    result = CliRunner().invoke(main, ["encode", str(tmp_path / "*.tif"), str(tmp_path / "o")])
    assert result.exit_code == 1
    assert "cannot find a date" in result.output


def test_geotiff_input_without_rasterio_names_the_geo_extra(tmp_path, monkeypatch):
    (tmp_path / "scene_2024-01-15.tif").write_bytes(b"")
    monkeypatch.setitem(sys.modules, "rasterio", None)  # makes `import rasterio` raise ImportError
    result = CliRunner().invoke(main, ["encode", str(tmp_path / "*.tif"), str(tmp_path / "o")])
    assert result.exit_code == 1
    assert "uv sync --extra geo" in result.output
    assert "chronozarr[geo]" in result.output


_DATES = ("S2_20240115", "S2_20240215", "S2_20240315")
_SPEC_DTYPES = ("uint8", "uint16", "int16", "float32")
_SENTINEL = {"uint8": 0, "uint16": 0, "int16": -32768, "float32": -9999.0}


def _random_frames(dtype: str, n_time: int = 3, n_band: int = 2) -> np.ndarray:
    """(time, band, 20, 30) values of `dtype` that never equal the `_SENTINEL` of that dtype."""
    rng = np.random.default_rng(11)
    shape = (n_time, n_band, 20, 30)
    if dtype == "float32":
        return rng.uniform(-50, 500, shape).astype("float32")
    info = np.iinfo(dtype)
    return rng.integers(info.min + 1, info.max + 1, shape).astype(dtype)


def _write_geotiffs(directory, names, frames, *, nodata=None, scale=None, transform=TRANSFORM):
    """One GeoTIFF per (band, y, x) frame, named `<name>.tif`; returns the glob that finds them."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import Affine

    directory.mkdir(exist_ok=True)
    for name, frame in zip(names, frames, strict=True):
        with rasterio.open(
            directory / f"{name}.tif",
            "w",
            driver="GTiff",
            height=frame.shape[1],
            width=frame.shape[2],
            count=frame.shape[0],
            dtype=frame.dtype.name,
            crs=CRS,
            transform=Affine(*transform),
            nodata=nodata,
        ) as dst:
            dst.write(frame)
            if scale is not None:
                dst.scales = [scale] * frame.shape[0]
                dst.offsets = [10.0] * frame.shape[0]
                dst.units = ["m"] * frame.shape[0]
    return str(directory / "*.tif")


@pytest.mark.parametrize("dtype", _SPEC_DTYPES)
def test_encode_geotiff_glob_keeps_dtype_nodata_scale_and_offset(tmp_path, dtype):
    frames = _random_frames(dtype)
    frames[:, :, :2, :2] = _SENTINEL[dtype]
    pattern = _write_geotiffs(
        tmp_path / "tifs", _DATES, frames, nodata=_SENTINEL[dtype], scale=0.5
    )
    out = tmp_path / "out"
    result = _run("encode", pattern, str(out), "--chunk-size", "16", "--crs", CRS)
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(out)
    values = store.to_xarray().values
    assert values.dtype == np.dtype(dtype)
    assert np.array_equal(values, frames)
    assert store.nodata == _SENTINEL[dtype]
    assert store.attrs.mask_variable is None
    assert [(b.scale, b.offset, b.units) for b in store.attrs.bands] == [(0.5, 10.0, "m")] * 2
    assert _run("validate", str(out)).exit_code == 0


@pytest.mark.parametrize("dtype", _SPEC_DTYPES)
def test_encode_geotiff_glob_without_declared_nodata_stores_none(tmp_path, dtype):
    frames = _random_frames(dtype)
    frames[:, :, :2, :2] = 0  # a zero is data when no nodata is declared, as in `convert`
    pattern = _write_geotiffs(tmp_path / "tifs", _DATES, frames)
    out = tmp_path / "out"
    result = _run("encode", pattern, str(out), "--chunk-size", "16")
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(out)
    assert store.nodata is None
    assert store.attrs.mask_variable is None
    assert np.array_equal(store.to_xarray().values, frames)


def _nan_frames() -> np.ndarray:
    """float32 frames with NaN in band 0 only: a pixel invalid in any band is invalid for all."""
    frames = _random_frames("float32")
    frames[:, 0, 3:5, 6:9] = np.nan
    return frames


def _expected_valid() -> np.ndarray:
    valid = np.ones((20, 30), dtype=np.uint8)
    valid[3:5, 6:9] = 0
    return valid


def _assert_nan_pixels_masked(store, frames, times):
    valid = _expected_valid()
    for t in times:
        stored = store.read(t)
        assert not np.isnan(stored).any()
        assert np.array_equal(store.read_mask(t), valid)
        assert (stored[0, 3:5, 6:9] == 0).all()
        assert np.array_equal(stored[0][valid == 1], frames[t, 0][valid == 1])
        assert np.array_equal(stored[1], frames[t, 1])


def test_encode_float32_geotiffs_with_nan_nodata_write_a_mask(tmp_path):
    frames = _nan_frames()
    pattern = _write_geotiffs(tmp_path / "tifs", _DATES, frames, nodata=float("nan"))
    out = tmp_path / "out"
    result = _run("encode", pattern, str(out), "--chunk-size", "16")
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(out)
    assert store.attrs.mask_variable is not None
    assert store.nodata is None
    _assert_nan_pixels_masked(store, frames, range(3))
    assert _run("validate", str(out)).exit_code == 0


def test_encode_float32_geotiffs_with_undeclared_nan_fail_naming_the_file(tmp_path):
    frames = _random_frames("float32")
    frames[1, 0, 0, 0] = np.nan
    pattern = _write_geotiffs(tmp_path / "tifs", _DATES, frames)
    out = tmp_path / "out"
    result = CliRunner().invoke(main, ["encode", pattern, str(out)])
    assert result.exit_code == 1
    assert "S2_20240215.tif" in result.output
    assert "NaN" in result.output
    assert not out.exists()


def test_append_float32_geotiffs_with_nan_nodata_continues_the_mask(tmp_path):
    frames = _nan_frames()
    first = _write_geotiffs(tmp_path / "first", _DATES[:2], frames[:2], nodata=float("nan"))
    later = _write_geotiffs(tmp_path / "later", _DATES[2:], frames[2:], nodata=float("nan"))
    out = tmp_path / "out"
    assert _run("encode", first, str(out), "--chunk-size", "16").exit_code == 0
    result = _run("append", str(out), later)
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(out)
    assert len(store.times) == 3
    _assert_nan_pixels_masked(store, frames, range(3))
    assert _run("validate", str(out)).exit_code == 0


def _tree(path):
    """Every file under `path` with its bytes."""
    return {p.relative_to(path): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def _encode_first_two(tmp_path, frames, **declared):
    """A store of the first two timesteps, whose files declare `declared` (see _write_geotiffs)."""
    pattern = _write_geotiffs(tmp_path / "first", _DATES[:2], frames[:2], **declared)
    out = tmp_path / "out"
    assert _run("encode", pattern, str(out), "--chunk-size", "16").exit_code == 0
    return out


def _later_files(tmp_path, frames, **declared):
    return _write_geotiffs(tmp_path / "later", _DATES[2:], frames[2:], **declared)


@pytest.fixture
def no_append(monkeypatch):
    """`append()` copies the whole store before it compares anything, so a refusal that is meant
    to cost nothing has to come before the call."""

    def refuse_to_run(*args, **kwargs):
        raise AssertionError("append() was called, so the store was copied before the refusal")

    monkeypatch.setattr("chronozarr.cli.append", refuse_to_run)


def _assert_append_refused(out, later, *fragments):
    before = _tree(out)
    result = CliRunner().invoke(main, ["append", str(out), later], catch_exceptions=False)
    assert result.exit_code == 1, result.output
    for fragment in fragments:
        assert fragment in result.output, result.output
    assert _tree(out) == before, "a refused append must leave the store untouched"


def test_append_geotiffs_declaring_another_nodata_is_refused(tmp_path, no_append):
    frames = _random_frames("uint16")
    out = _encode_first_two(tmp_path, frames, nodata=0)
    later = _later_files(tmp_path, frames, nodata=1)
    _assert_append_refused(
        out,
        later,
        "the files have nodata 1, the store has nodata 0",
        "gdal_edit.py -a_nodata 0",
        "encode it again from all its files",
    )


def test_append_geotiffs_declaring_no_nodata_to_a_store_with_one_is_refused(tmp_path, no_append):
    frames = _random_frames("uint16")
    out = _encode_first_two(tmp_path, frames, nodata=0)
    later = _later_files(tmp_path, frames)
    _assert_append_refused(
        out,
        later,
        "the files have no nodata, the store has nodata 0",
        "gdal_edit.py -a_nodata 0",
    )


def test_append_geotiffs_declaring_nodata_to_a_store_with_none_is_refused(tmp_path, no_append):
    frames = _random_frames("uint16")
    out = _encode_first_two(tmp_path, frames)
    later = _later_files(tmp_path, frames, nodata=0)
    _assert_append_refused(
        out,
        later,
        "the files have nodata 0, the store has no nodata",
        "gdal_edit.py -unsetnodata",
    )


def test_append_geotiffs_without_a_mask_to_a_masked_store_is_refused(tmp_path, no_append):
    out = _encode_first_two(tmp_path, _nan_frames(), nodata=float("nan"))
    assert chronozarr.open_store(out).attrs.mask_variable is not None
    later = _later_files(tmp_path, _random_frames("float32"))  # no NaN, no nodata: no mask
    _assert_append_refused(
        out,
        later,
        "the store has a mask and the files give none",
        "alpha band, an internal mask, or NaN declared as nodata",
    )


def test_append_geotiffs_with_a_mask_to_a_store_without_one_is_refused(tmp_path, no_append):
    out = _encode_first_two(tmp_path, _random_frames("float32"), nodata=-9999.0)
    assert chronozarr.open_store(out).attrs.mask_variable is None
    later = _later_files(tmp_path, _nan_frames(), nodata=float("nan"))
    _assert_append_refused(
        out,
        later,
        "the files give a mask",
        "the store has none, which append cannot add",
        "the files have no nodata, the store has nodata -9999.0",
    )


@pytest.mark.parametrize("declared", [{"nodata": 0}, {}], ids=["nodata-0", "no-nodata"])
def test_append_geotiffs_with_the_stores_validity_rule_is_accepted(tmp_path, declared):
    frames = _random_frames("uint16")
    out = _encode_first_two(tmp_path, frames, **declared)
    later = _later_files(tmp_path, frames, **declared)
    result = _run("append", str(out), later)
    assert result.exit_code == 0, result.output

    store = chronozarr.open_store(out)
    assert len(store.times) == 3
    assert store.nodata == declared.get("nodata")
    assert np.array_equal(store.to_xarray().values, frames)
    assert _run("validate", str(out)).exit_code == 0


def test_encode_geotiff_glob_rejects_files_of_different_dtypes(tmp_path):
    frames = [
        _random_frames("uint16")[0],
        _random_frames("float32")[0],
        _random_frames("uint16")[2],
    ]
    pattern = _write_geotiffs(tmp_path / "tifs", _DATES, frames)
    result = CliRunner().invoke(main, ["encode", pattern, str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "S2_20240215.tif is float32" in result.output
    assert "S2_20240115.tif is uint16" in result.output


@pytest.mark.parametrize("dtype", ["float64", "int32"])
def test_encode_geotiff_glob_names_an_unsupported_dtype(tmp_path, dtype):
    frames = _random_frames("float32").astype(dtype)
    pattern = _write_geotiffs(tmp_path / "tifs", _DATES, frames)
    result = CliRunner().invoke(main, ["encode", pattern, str(tmp_path / "out")])
    assert result.exit_code == 1
    assert dtype in result.output
    assert "uint8" in result.output
    assert "gdal_translate" in result.output


def test_encode_geotiff_glob_does_not_resample_a_file_off_the_grid(tmp_path):
    frames = _random_frames("uint16")
    _write_geotiffs(tmp_path / "tifs", _DATES[:2], frames[:2])
    shifted = (*TRANSFORM[:2], TRANSFORM[2] + 10.0, *TRANSFORM[3:])
    pattern = _write_geotiffs(tmp_path / "tifs", _DATES[2:], frames[2:], transform=shifted)
    result = CliRunner().invoke(main, ["encode", pattern, str(tmp_path / "out")])
    assert result.exit_code == 1
    assert "S2_20240315.tif" in result.output
    assert "does not resample" in result.output


def test_encode_geotiff_glob_does_not_reproject_to_another_crs(tmp_path):
    pattern = _write_geotiffs(tmp_path / "tifs", _DATES, _random_frames("uint16"))
    result = CliRunner().invoke(
        main, ["encode", pattern, str(tmp_path / "out"), "--crs", "EPSG:32632"]
    )
    assert result.exit_code == 1
    assert "--crs EPSG:32632" in result.output
    assert "does not reproject" in result.output


def test_doctor_passes_on_a_local_store(tmp_path, zarr_input):
    source, _ = zarr_input
    out = tmp_path / "out"
    assert _run("encode", str(source), str(out), "--chunk-size", "16").exit_code == 0
    result = _run("doctor", str(out))
    assert result.exit_code == 0, result.output
    assert "[ ok ] validate" in result.output
    assert "[ ok ] decode level 0" in result.output
    assert "0 failure(s)" in result.output


def test_doctor_exits_nonzero_with_a_fix_line(tmp_path):
    result = CliRunner().invoke(main, ["doctor", str(tmp_path / "missing")])
    assert result.exit_code == 1
    assert "[FAIL] store path" in result.output
    assert "fix: Pass the store directory" in result.output
    assert "1 failure(s)" in result.output


def test_export_cog_selects_levels_and_times(tmp_path, zarr_input):
    rasterio = pytest.importorskip("rasterio")
    source, truth = zarr_input
    store_dir = tmp_path / "store"
    assert _run("encode", str(source), str(store_dir), "--chunk-size", "16").exit_code == 0
    out = tmp_path / "cogs"
    result = _run("export-cog", str(store_dir), str(out), "--times", "1:3", "--times", "-1")
    assert result.exit_code == 0, result.output
    assert "wrote 3 COG(s)" in result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "L0_2024-02-01.tif",
        "L0_2024-03-01.tif",
        "L0_2024-04-01.tif",
    ]
    with rasterio.open(out / "L0_2024-03-01.tif") as src:
        assert np.array_equal(src.read(), truth[2])

    coarse = _run("export-cog", str(store_dir), str(tmp_path / "coarse"), "--level", "1")
    assert coarse.exit_code == 0, coarse.output
    assert "wrote 4 COG(s)" in coarse.output


def test_export_cog_reports_bad_input_as_click_errors(tmp_path, zarr_input):
    pytest.importorskip("rasterio")
    source, _ = zarr_input
    store_dir = tmp_path / "store"
    assert _run("encode", str(source), str(store_dir), "--chunk-size", "16").exit_code == 0
    bad_time = CliRunner().invoke(
        main, ["export-cog", str(store_dir), str(tmp_path / "o"), "--times", "2030-01"]
    )
    assert bad_time.exit_code == 1
    assert "no timestep matches" in bad_time.output
    bad_level = CliRunner().invoke(
        main, ["export-cog", str(store_dir), str(tmp_path / "o"), "--level", "9"]
    )
    assert bad_level.exit_code == 1
    assert "level 9 out of range" in bad_level.output


def test_stac_writes_collection_and_item(tmp_path, zarr_input):
    pytest.importorskip("rasterio")
    source, _ = zarr_input
    store_dir = tmp_path / "aoi" / "chronozarr"
    store_dir.parent.mkdir()
    assert _run("encode", str(source), str(store_dir), "--chunk-size", "16").exit_code == 0
    out = tmp_path / "catalog"
    result = _run(
        "stac", str(store_dir), "--out", str(out), "--license", "CC-BY-4.0", "--title", "AOI"
    )
    assert result.exit_code == 0, result.output
    assert f"wrote {out / 'collection.json'}" in result.output
    assert (out / "aoi-chronozarr" / "aoi-chronozarr.json").is_file()

    missing = CliRunner().invoke(main, ["stac", str(tmp_path / "nope"), "--out", str(out)])
    assert missing.exit_code == 1
    assert "no Zarr v3 group found" in missing.output


def _write_manifest(tmp_path, truth):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import Affine

    folder = tmp_path / "cogs"
    folder.mkdir()
    lines = ["uri,datetime,bands"]
    for t in range(truth.shape[0]):
        path = folder / f"scene_{t}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=truth.shape[2],
            width=truth.shape[3],
            count=truth.shape[1],
            dtype="uint16",
            crs="EPSG:32631",
            transform=Affine(10.0, 0.0, 746090.0, 0.0, -10.0, 2540440.0),
            nodata=0,
        ) as dst:
            dst.write(truth[t])
        lines.append(f"{path},2024-0{t + 1}-01,red;nir")
    manifest = tmp_path / "manifest.csv"
    manifest.write_text("\n".join(lines) + "\n")
    return manifest


def test_convert_manifest_end_to_end(tmp_path):
    truth = make_truth(4, 2, 40, 50)
    manifest = _write_manifest(tmp_path, truth)
    out = tmp_path / "store"
    result = _run("convert", str(manifest), str(out), "--chunk-size", "16")
    assert result.exit_code == 0, result.output
    for expected in (
        "source:     manifest of COGs, 4 timesteps (2024-01-01 .. 2024-04-01)",
        "resampling: none needed",
        "output:     about",
        "wrote ",
        "read 4 timesteps (0 reused)",
    ):
        assert expected in result.output
    store = chronozarr.open_store(out)
    assert store.bands == ("red", "nir")
    assert np.array_equal(store.to_xarray().values, truth)
    assert not (tmp_path / "store.convert-work").exists()


def test_convert_dry_run_writes_nothing(tmp_path):
    manifest = _write_manifest(tmp_path, make_truth(3, 2, 40, 50))
    out = tmp_path / "store"
    result = _run("convert", str(manifest), str(out), "--dry-run", "--chunk-size", "16")
    assert result.exit_code == 0, result.output
    assert "raw size:" in result.output
    assert "dry run: nothing was written" in result.output
    assert not out.exists()


def test_convert_reports_errors_as_click_errors(tmp_path):
    truth = make_truth(3, 2, 40, 50)
    manifest = _write_manifest(tmp_path, truth)
    missing = CliRunner().invoke(
        main, ["convert", str(tmp_path / "nope.csv"), str(tmp_path / "o")]
    )
    assert missing.exit_code == 1
    assert "does not exist" in missing.output

    off_grid = CliRunner().invoke(
        main,
        ["convert", str(manifest), str(tmp_path / "o"), "--crs", "EPSG:32632", "--dry-run"],
    )
    assert off_grid.exit_code == 1
    assert "not on the target grid" in off_grid.output
    assert "--resampling" in off_grid.output

    bad_shape = CliRunner().invoke(
        main, ["convert", str(manifest), str(tmp_path / "o"), "--shape", "5"]
    )
    assert bad_shape.exit_code == 2
    assert "--shape needs 2 comma-separated numbers" in bad_shape.output


def test_convert_resampling_warps_to_a_new_crs(tmp_path):
    manifest = _write_manifest(tmp_path, make_truth(3, 2, 40, 50))
    out = tmp_path / "store"
    result = _run(
        "convert",
        str(manifest),
        str(out),
        "--crs",
        "EPSG:32632",
        "--resampling",
        "nearest",
        "--chunk-size",
        "16",
    )
    assert result.exit_code == 0, result.output
    assert "resampling: 3 of 3 timesteps are warped" in result.output
    assert chronozarr.open_store(out).attrs.crs == "EPSG:32632"
