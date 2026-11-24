"""`export_cog`: decoded true-value COGs readable by GDAL."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import numpy as np
import pytest
import zarr

import chronozarr
from chronozarr.convert import convert
from chronozarr.export import _file_stem, export_cog, select_times
from tests.fixtures import cog_sources as fx
from tests.synthetic import BANDS, build_store, make_da, make_truth

pytestmark = pytest.mark.unit

TIMES = [
    "2024-01-01T00:00:00Z",
    "2024-02-01T00:00:00Z",
    "2024-03-01T00:00:00Z",
    "2024-03-20T00:00:00Z",
    "2025-01-15T00:00:00Z",
]


# --- select_times ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ([], [0, 1, 2, 3, 4]),
        (["all"], [0, 1, 2, 3, 4]),
        (["0,2"], [0, 2]),
        (["4", "1"], [1, 4]),
        (["-1"], [4]),
        (["1:3"], [1, 2]),
        (["::2"], [0, 2, 4]),
        (["2024-03"], [2, 3]),
        (["2024-03-20"], [3]),
        (["2024-02..2024-03"], [1, 2, 3]),
        (["2024-03..2025-01"], [2, 3, 4]),
        (["0, 2024-03"], [0, 2, 3]),
    ],
)
def test_select_times(spec, expected):
    assert select_times(spec, TIMES) == expected


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (["7"], "out of range"),
        (["-6"], "out of range"),
        (["2030-01"], "no timestep matches"),
        (["2024"], "out of range"),  # a bare number is an index, not a year
        (["a:b"], "bad index slice"),
        (["2024..2025"], "bad time range"),
        (["banana"], "cannot parse"),
        (["5:9"], "no timesteps selected"),
    ],
)
def test_select_times_rejects_bad_tokens(spec, message):
    with pytest.raises(ValueError, match=message):
        select_times(spec, TIMES)


@pytest.mark.parametrize("separator", ["T", " "])
def test_select_times_accepts_timestamp_prefixes_and_ranges(separator):
    times = [
        "2024-03-01T12:30:00Z",
        "2024-03-01T12:30:30Z",
        "2024-03-01T13:00:00Z",
    ]
    assert select_times([f"2024-03-01{separator}12:30:00Z"], times) == [0]
    assert select_times([f"2024-03-01{separator}12:30"], times) == [0, 1]
    assert select_times(
        [f"2024-03-01{separator}12:30:00Z..2024-03-01{separator}12:30:30Z"], times
    ) == [0, 1]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (["2024-03-01T12:30:00Z"], [0]),
        (["2024-03-01T12:30:00.0000Z"], [0]),
        (["2024-03-01T12:30:00"], [0, 1, 2]),
        (["2024-03-01T12:30:00.1"], [1, 2]),
        (["2024-03-01T12:30:00.1Z"], [1]),
        (["2024-03-01T12:30:00Z..2024-03-01T12:30:30Z"], [0, 1, 2, 3]),
        (["2024-03-01T12:30:00Z..2024-03-01T12:30"], [0, 1, 2, 3, 4]),
        (["2024-03-01T12:30:00.1Z..2024-03-01T12:30:00.12Z"], [1, 2]),
    ],
)
def test_select_times_compares_equivalent_utc_precision(spec, expected):
    times = [
        "2024-03-01T12:30:00.000Z",
        "2024-03-01T12:30:00.100Z",
        "2024-03-01T12:30:00.120Z",
        "2024-03-01T12:30:30.000Z",
        "2024-03-01T12:30:30.001Z",
    ]
    assert select_times(spec, times) == expected


def test_select_times_accepts_fractional_zero_for_whole_second_metadata():
    assert select_times(["2024-03-01T12:30:00.000Z"], ["2024-03-01T12:30:00Z"]) == [0]


def test_file_stem_keeps_the_clock_only_when_needed():
    assert _file_stem("2024-03-01T00:00:00Z") == "2024-03-01"
    assert _file_stem("2024-03-01T00:00:00.000Z") == "2024-03-01"
    assert _file_stem("2024-03-01T12:30:00Z") == "2024-03-01T123000Z"


# --- export_cog -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def store_and_truth(tmp_path_factory):
    truth = make_truth(5, 2, 40, 50)
    path = tmp_path_factory.mktemp("export") / "store"
    build_store(path, truth, shard=True, chunk_size=16)
    return path, truth


def test_every_timestep_roundtrips_through_rasterio(store_and_truth, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    path, truth = store_and_truth
    paths = export_cog(path, tmp_path / "cogs")
    assert [p.name for p in paths] == [
        "L0_2024-01-01.tif",
        "L0_2024-02-01.tif",
        "L0_2024-03-01.tif",
        "L0_2024-04-01.tif",
        "L0_2024-05-01.tif",
    ]
    store = chronozarr.open_store(path)
    for t, tif in enumerate(paths):
        with rasterio.open(tif) as src:
            assert np.array_equal(src.read(), truth[t])
            assert src.crs.to_string() == store.attrs.crs
            assert tuple(src.transform)[:6] == store.levels[0].transform
            assert src.descriptions == tuple(BANDS)
            assert src.nodata == 0
            assert src.dtypes == ("uint16", "uint16")
            assert src.tags()["CHRONOZARR_TIME"] == store.attrs.times[t]
            assert src.tags()["CHRONOZARR_TIMESTEP"] == str(t)
            assert src.profile["tiled"]
            assert src.compression.name == "deflate"


def test_level_and_time_selection(store_and_truth, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    path, _ = store_and_truth
    store = chronozarr.open_store(path)
    assert len(store.levels) > 1
    paths = export_cog(store, tmp_path, level=1, times=[3])
    assert [p.name for p in paths] == ["L1_2024-04-01.tif"]
    with rasterio.open(paths[0]) as src:
        assert np.array_equal(src.read(), store.read(3, 1))
        assert tuple(src.transform)[:6] == store.levels[1].transform
        assert src.tags()["CHRONOZARR_LEVEL"] == "1"


def test_foreign_overview_export_uses_its_own_missing_value(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    path = tmp_path / "foreign.zarr"
    root = zarr.open_group(path, mode="w", zarr_format=3)
    root.attrs["multiscales"] = {"layout": [{"asset": "0"}, {"asset": "1"}]}
    for level, marker in enumerate((-32768, -9999)):
        group = root.create_group(str(level))
        size, step = 4 // 2**level, 2**level
        group.attrs.update(
            {"proj:code": "EPSG:4326", "spatial:transform": [step, 0, 0, 0, -step, 4]}
        )
        data = group.create_array(
            "data",
            shape=(size, size),
            dtype="int16",
            dimension_names=("y", "x"),
            attributes={"_FillValue": marker},
        )
        data[:] = -32768  # valid in the overview, missing at the finest level
        data[0, 0] = marker

    store = chronozarr.open_store(path)
    assert np.isnan(store.physical(0, 1)[0, 0, 0])
    (target,) = export_cog(store, tmp_path / "cogs", level=1, times=[0])
    with rasterio.open(target) as src:
        assert src.nodata == -9999
        np.testing.assert_array_equal(src.read(1), store.read(0, 1)[0])
        assert src.read_masks(1)[0, 0] == 0
        assert src.read_masks(1)[0, 1] == 255


def test_foreign_subsecond_frames_export_to_distinct_files(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    root = zarr.open_group(tmp_path / "foreign.zarr", mode="w", zarr_format=3)
    root.attrs["multiscales"] = {"layout": [{"asset": "0"}]}
    group = root.create_group("0")
    group.attrs.update({"proj:code": "EPSG:4326", "spatial:transform": [1, 0, 0, 0, -1, 2]})
    truth = np.stack([np.full((2, 2), value, dtype="uint16") for value in (1, 2)])
    group.create_array("data", data=truth, chunks=(1, 2, 2), dimension_names=("time", "y", "x"))
    clock = group.create_array(
        "time", data=np.array([100, 200], dtype="int64"), dimension_names=("time",)
    )
    clock.attrs.update(
        {"units": "milliseconds since 2024-01-01", "calendar": "proleptic_gregorian"}
    )

    store = chronozarr.open_store(tmp_path / "foreign.zarr")
    paths = export_cog(store, tmp_path / "cogs")
    assert [path.name for path in paths] == [
        "L0_2024-01-01T000000.100Z.tif",
        "L0_2024-01-01T000000.200Z.tif",
    ]
    assert len(list((tmp_path / "cogs").glob("*.tif"))) == 2
    for t, path in enumerate(paths):
        with rasterio.open(path) as src:
            np.testing.assert_array_equal(src.read(1), truth[t])
            assert src.tags()["CHRONOZARR_TIME"] == store.attrs.times[t]
            assert src.tags()["CHRONOZARR_TIMESTEP"] == str(t)


def test_duplicate_export_targets_are_rejected_before_writing(store_and_truth, tmp_path):
    pytest.importorskip("rasterio")
    path, _ = store_and_truth
    out = tmp_path / "cogs"
    with pytest.raises(ValueError, match="duplicate output"):
        export_cog(path, out, times=[0, 0])
    assert not out.exists()


def test_existing_files_and_bad_level_are_errors(store_and_truth, tmp_path):
    pytest.importorskip("rasterio")
    path, _ = store_and_truth
    export_cog(path, tmp_path, times=[0])
    with pytest.raises(FileExistsError, match="already exist"):
        export_cog(path, tmp_path, times=[0])
    with pytest.raises(ValueError, match="level 9 out of range"):
        export_cog(path, tmp_path / "other", level=9)


def test_export_preserves_a_target_created_after_its_initial_check(
    store_and_truth, tmp_path, monkeypatch
):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import Affine

    path, _ = store_and_truth
    store = chronozarr.open_store(path)
    out = tmp_path / "cogs"
    target = out / f"L0_{_file_stem(store.attrs.times[0])}.tif"
    assert not target.exists()
    sentinel = np.full((1, 2, 3), 54321, dtype=np.uint16)
    created = []
    read = store.read

    def read_after_precheck(t, lod=0):
        if not created:
            assert out.is_dir(), "export has passed its initial target check"
            assert not target.exists()
            with rasterio.open(
                target,
                "w",
                driver="GTiff",
                width=3,
                height=2,
                count=1,
                dtype="uint16",
                crs=store.crs.to_string(),
                transform=Affine(*store.levels[0].transform),
            ) as dst:
                dst.write(sentinel)
            created.append(target.read_bytes())
        return read(t, lod)

    # Insert another writer's valid TIFF after preflight; keep the actual COG copy unchanged.
    monkeypatch.setattr(store, "read", read_after_precheck)
    refusal = None
    try:
        export_cog(store, out, times=[0])
    except (FileExistsError, rasterio.errors.RasterioIOError) as error:
        refusal = error
    assert len(created) == 1, "the collision was introduced during the first pixel read"
    assert target.read_bytes() == created[0], "another writer's TIFF must remain byte-identical"
    with rasterio.open(target) as src:
        np.testing.assert_array_equal(src.read(), sentinel)
    assert refusal is not None, "the export must clearly refuse the existing target"
    if not isinstance(refusal, FileExistsError):
        assert any(word in str(refusal).lower() for word in ("exist", "overwrit", "occupied"))


def test_export_publishes_a_complete_sibling_with_normal_permissions(
    store_and_truth, tmp_path, monkeypatch
):
    rasterio = pytest.importorskip("rasterio")
    path, truth = store_and_truth
    out = tmp_path / "cogs"
    out.mkdir()
    control = out / "normal-permissions"
    control.write_bytes(b"")
    expected_mode = stat.S_IMODE(control.stat().st_mode)
    control.unlink()
    link = os.link
    published = []

    def publish(temporary, target):
        assert temporary.parent == target.parent == out
        assert not target.exists(), "the final path stays absent while the COG is written"
        with rasterio.open(temporary) as src:
            np.testing.assert_array_equal(src.read(), truth[0])
            assert src.tags()["CHRONOZARR_TIME"] == "2024-01-01T00:00:00Z"
        assert stat.S_IMODE(temporary.stat().st_mode) == expected_mode
        published.append(target)
        link(temporary, target)

    monkeypatch.setattr(os, "link", publish)
    targets = export_cog(path, out, times=[0])
    assert published == targets
    assert list(out.iterdir()) == targets, "the owned temporary name is removed after publication"
    assert stat.S_IMODE(targets[0].stat().st_mode) == expected_mode


@pytest.mark.parametrize("failure_at", ["copy", "publication"])
def test_export_cleans_private_files_after_write_or_publication_failure(
    store_and_truth, tmp_path, monkeypatch, failure_at
):
    pytest.importorskip("rasterio")
    import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub

    path, _ = store_and_truth
    out = tmp_path / "cogs"
    code = errno.EIO if failure_at == "copy" else getattr(errno, "ENOTSUP", errno.EPERM)
    reason = "copy failed" if failure_at == "copy" else "hard links not supported"
    failure = OSError(code, reason)
    temporary_paths = []

    def fail_copy(source, temporary, **options):
        temporary_paths.append(Path(temporary))
        Path(temporary).write_bytes(b"incomplete COG")
        raise failure

    def fail_publication(temporary, target):
        temporary_paths.append(Path(temporary))
        raise failure

    if failure_at == "copy":
        monkeypatch.setattr(rasterio.shutil, "copy", fail_copy)
    else:
        monkeypatch.setattr(os, "link", fail_publication)
    with pytest.raises(OSError) as raised:
        export_cog(path, out, times=[0])
    assert raised.value is failure, "cleanup must preserve the original error"
    assert len(temporary_paths) == 1
    assert temporary_paths[0].parent == out
    assert list(out.iterdir()) == [], "a failed write or link publishes no final TIFF"


def test_export_preserves_the_write_error_when_private_cleanup_also_fails(
    store_and_truth, tmp_path, monkeypatch
):
    pytest.importorskip("rasterio")
    import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub

    path, _ = store_and_truth
    out = tmp_path / "cogs"
    failure = OSError(errno.EIO, "COG write failed")
    unlink = Path.unlink

    def fail_copy(source, temporary, **options):
        Path(temporary).write_bytes(b"incomplete COG")
        raise failure

    def fail_cleanup(temporary, *args, **options):
        if temporary.parent == out:
            raise PermissionError(errno.EACCES, "cleanup blocked", temporary)
        return unlink(temporary, *args, **options)

    with monkeypatch.context() as patch:
        patch.setattr(rasterio.shutil, "copy", fail_copy)
        patch.setattr(Path, "unlink", fail_cleanup)
        with pytest.raises(OSError) as raised:
            export_cog(path, out, times=[0])
        assert raised.value is failure
        assert any("could not remove temporary COG" in note for note in failure.__notes__)
        remaining = list(out.iterdir())
        assert len(remaining) == 1 and remaining[0].name.startswith(".chronozarr-export-")
    # The cleanup failure was injected only for the assertion above.
    remaining[0].unlink()


@pytest.mark.skipif(shutil.which("gdalinfo") is None, reason="GDAL command line not installed")
def test_gdalinfo_reports_a_cloud_optimized_layout(store_and_truth, tmp_path):
    pytest.importorskip("rasterio")
    path, truth = store_and_truth
    (tif,) = export_cog(path, tmp_path, times=[2])
    out = subprocess.run(
        ["gdalinfo", "-json", "-stats", str(tif)], check=True, capture_output=True, text=True
    )
    info = json.loads(out.stdout)
    assert info["metadata"]["IMAGE_STRUCTURE"]["LAYOUT"] == "COG"
    assert info["size"] == [50, 40]
    assert [b["description"] for b in info["bands"]] == BANDS
    assert info["bands"][0]["noDataValue"] == 0
    assert info["bands"][0]["type"] == "UInt16"
    valid = truth[2, 0][truth[2, 0] != 0]
    assert info["bands"][0]["maximum"] == float(valid.max())


# --- fidelity: validity, scale, offset, units -----------------------------------------------

# builder -> (one plane for all bands, GeoTIFF nodata, per-dataset mask in the export)
ROUND_TRIPS = {
    "scaled_masked": (fx.scaled_masked, True, None, True),
    "rgba": (fx.rgba, True, None, True),
    "nodata_zero": (fx.nodata_zero, False, 0, False),
    "valid_zero": (fx.valid_zero, False, None, False),
    "int16_negative": (fx.int16_negative, False, -9999, False),
    "float32_nan": (fx.float32_nan, True, None, True),
    "float32_finite_nodata": (fx.float32_finite_nodata, False, -9999.0, False),
    "nodata_changes": (fx.nodata_changes, True, None, True),
}


def band_validity(src) -> np.ndarray:
    """(band, y, x) bool: what GDAL says is valid in each band."""
    return np.stack([src.read_masks(b) > 0 for b in range(1, src.count + 1)])


@pytest.mark.parametrize("name", list(ROUND_TRIPS))
def test_convert_then_export_cog_preserves_validity_scale_offset_units_and_values(name, tmp_path):
    rasterio = pytest.importorskip("rasterio")
    build, shared, nodata, has_mask = ROUND_TRIPS[name]
    cogs = build(tmp_path)
    store_path = tmp_path / "store"
    convert(fx.write_manifest(tmp_path / "m.csv", cogs), store_path, chunk_size=16)
    tifs = export_cog(store_path, tmp_path / "out")
    assert len(tifs) == fx.N_TIME
    n_band = cogs.data.shape[1]
    names = tuple(d or str(i + 1) for i, d in enumerate(cogs.descriptions))
    for t, tif in enumerate(tifs):
        expected_valid = (
            np.broadcast_to(cogs.shared_valid[t], cogs.valid[t].shape) if shared else cogs.valid[t]
        )
        # a NaN is never stored; it is the marker of an invalid pixel and reads back as 0
        expected_values = np.where(np.isnan(cogs.data[t]), 0, cogs.data[t])
        with rasterio.open(tif) as out:
            assert out.dtypes == (cogs.data.dtype.name,) * n_band
            assert np.array_equal(out.read(), expected_values)
            assert np.array_equal(band_validity(out), expected_valid)
            assert out.scales == cogs.scales
            assert out.offsets == cogs.offsets
            assert out.units == cogs.units
            assert out.descriptions == names  # unnamed bands are called 1, 2, ... by convert
            assert out.nodata == nodata
            flags = {flag.name for flag in out.mask_flag_enums[0]}
            assert ("per_dataset" in flags) is has_mask
            assert out.tags()["CHRONOZARR_TIMESTEP"] == str(t)
        with rasterio.open(cogs.paths[t]) as src:
            if np.array_equal(cogs.valid[t], expected_valid):  # bands agree on validity
                data_bands = [b for b in range(1, src.count + 1) if b <= n_band]
                source_valid = np.stack([src.read_masks(b) > 0 for b in data_bands])
                assert np.array_equal(source_valid, expected_valid)  # GDAL's own view too


@pytest.mark.parametrize("flags", [{}, {"missing": -8888, "flag_var": True}])
def test_zarr_with_cf_attributes_roundtrips_to_cogs(tmp_path, flags):
    rasterio = pytest.importorskip("rasterio")
    zarr_set = fx.cf_zarr(tmp_path / "in.zarr", **flags)
    store_path = tmp_path / "store"
    kwargs = {"variable": "v", "mask_var": "ok"} if flags else {}
    convert(zarr_set.path, store_path, chunk_size=16, **kwargs)
    shared = bool(flags)
    for t, tif in enumerate(export_cog(store_path, tmp_path / "out")):
        expected = (
            np.broadcast_to(zarr_set.shared_valid[t], zarr_set.valid[t].shape)
            if shared
            else zarr_set.valid[t]
        )
        with rasterio.open(tif) as out:
            assert np.array_equal(out.read(), zarr_set.data[t])
            assert np.array_equal(band_validity(out), expected)
            assert out.scales == (0.01, 0.01)
            assert out.offsets == (1.5, 1.5)
            assert out.units == ("m", "m")
            assert out.descriptions == zarr_set.bands
            assert out.nodata == (None if shared else -9999)


def masked_store(tmp_path, truth, mask, **encode_kwargs):
    # A mask store has no nodata by default; tests about the nodata tag next to a mask ask for 0.
    path = tmp_path / "store"
    chronozarr.encode(make_da(truth, BANDS), path, mask=mask, chunk_size=16, **encode_kwargs)
    return path


def test_export_keeps_a_valid_zero_and_an_invalid_nonzero_pixel(tmp_path):
    """The reviewer's case: nodata 0 plus a mask, with a valid zero and an invalid 777."""
    rasterio = pytest.importorskip("rasterio")
    truth = make_truth(4, 2, 40, 50)
    mask = np.ones((4, 40, 50), dtype=np.uint8)
    truth[:, :, 5, 6] = 0  # valid zero
    truth[:, :, 3, 4] = 777  # invalid, not zero
    mask[:, 3, 4] = 0
    path = masked_store(tmp_path, truth, mask, nodata=0)
    assert chronozarr.open_store(path).attrs.nodata == 0
    for t, tif in enumerate(export_cog(path, tmp_path / "cogs")):
        with rasterio.open(tif) as src:
            assert np.array_equal(src.read(), truth[t])
            assert np.array_equal(band_validity(src), np.broadcast_to(mask[t] > 0, (2, 40, 50)))
            assert src.nodata is None  # a valid pixel holds 0, so the tag would hide data


def test_export_keeps_the_nodata_tag_next_to_a_mask_when_no_valid_pixel_holds_it(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    truth = make_truth(4, 2, 40, 50)  # its only zeros are the nodata corner
    mask = (truth != 0).all(axis=1).astype(np.uint8)
    path = masked_store(tmp_path, truth, mask, nodata=0)
    for t, tif in enumerate(export_cog(path, tmp_path / "cogs")):
        with rasterio.open(tif) as src:
            assert src.nodata == 0
            assert "per_dataset" in {f.name for f in src.mask_flag_enums[0]}
            assert np.array_equal(band_validity(src), np.broadcast_to(mask[t] > 0, (2, 40, 50)))


@pytest.mark.parametrize(
    ("fill", "use_mask", "physical", "has_overviews"),
    [
        (0, False, False, False),
        (-32768, False, False, True),
        (0, True, False, True),
        (0, False, True, True),
        (0, True, True, True),
    ],
)
def test_export_keeps_valid_means_separate_from_nodata(
    tmp_path, fill, use_mask, physical, has_overviews
):
    rasterio = pytest.importorskip("rasterio")
    truth = np.ones((1, 1, 520, 520), dtype=np.int16)
    truth[0, 0, :2, :2] = [[-2, 2], [-2, 2]]
    truth[0, 0, 2:4, 2:4] = fill
    mask = truth[:, 0] != fill if use_mask else None
    path = tmp_path / "store"
    chronozarr.encode(make_da(truth, ["v"]), path, nodata=fill, mask=mask, n_lods=1)
    (target,) = export_cog(path, tmp_path / "cogs", physical=physical)
    with rasterio.open(target) as src:
        if physical:
            assert np.isnan(src.nodata)
        else:
            assert src.nodata == (None if use_mask else fill)
        base = src.read(1, window=((0, 4), (0, 4)), masked=True)
        np.testing.assert_array_equal(base[:2, :2], [[-2, 2], [-2, 2]])
        assert base.mask[2:4, 2:4].all()
        assert src.overviews(1) == ([2] if has_overviews else [])
        if has_overviews:
            overview = src.read(1, out_shape=(260, 260), masked=True)
            assert overview[0, 0] == 0
            assert overview.mask[1, 1]


def test_export_decides_the_nodata_tag_per_timestep(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    truth = make_truth(4, 2, 40, 50)
    mask = (truth != 0).all(axis=1).astype(np.uint8)
    truth[2, :, 5, 6] = 0  # a valid zero in timestep 2 only
    mask[2, 5, 6] = 1
    tifs = export_cog(masked_store(tmp_path, truth, mask, nodata=0), tmp_path / "cogs")
    tags = []
    for tif in tifs:
        with rasterio.open(tif) as src:
            tags.append(src.nodata)
    assert tags == [0, 0, None, 0]


def test_export_uses_the_mask_of_the_requested_level(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    truth = make_truth(4, 2, 40, 50)
    mask = np.ones((4, 40, 50), dtype=np.uint8)
    mask[:, :8, :8] = 0
    path = masked_store(tmp_path, truth, mask)
    store = chronozarr.open_store(path)
    (tif,) = export_cog(store, tmp_path / "cogs", level=1, times=[1])
    level_mask = store.read_mask(1, 1)
    assert level_mask is not None
    with rasterio.open(tif) as src:
        assert np.array_equal(band_validity(src)[0], level_mask > 0)
        assert (band_validity(src)[0] == 0).any()
        assert np.array_equal(src.read(), store.read(1, 1))


def test_a_store_without_nodata_or_mask_exports_neither(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    truth = make_truth(3, 2, 40, 50)  # contains zeros, which are data here
    path = tmp_path / "store"
    chronozarr.encode(make_da(truth, BANDS), path, nodata=None, chunk_size=16)
    for t, tif in enumerate(export_cog(path, tmp_path / "cogs")):
        with rasterio.open(tif) as src:
            assert src.nodata is None
            assert band_validity(src).all()
            assert {f.name for f in src.mask_flag_enums[0]} == {"all_valid"}
            assert np.array_equal(src.read(), truth[t])


def test_export_preserves_zero_scale_and_its_physical_values(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    truth = np.full((1, 1, 16, 16), 7, dtype=np.uint16)
    path = tmp_path / "store"
    chronozarr.encode(
        make_da(truth, ["constant"]),
        path,
        bands=[{"name": "constant", "scale": 0.0, "offset": 3.0}],
        nodata=None,
        chunk_size=16,
    )
    store = chronozarr.open_store(path)
    (stored_tif,) = export_cog(store, tmp_path / "stored")
    with rasterio.open(stored_tif) as src:
        assert src.scales == (0.0,)
        assert src.offsets == (3.0,)
        assert np.array_equal(src.read(), truth[0])
        assert np.array_equal(src.read() * src.scales[0] + src.offsets[0], store.physical(0))
    (physical_tif,) = export_cog(store, tmp_path / "physical", physical=True)
    with rasterio.open(physical_tif) as src:
        assert np.array_equal(src.read(), np.full((1, 16, 16), 3.0, dtype=np.float32))
        assert src.scales == (1.0,)
        assert src.offsets == (0.0,)


def test_physical_export_writes_float32_values_with_nan_where_invalid(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    cogs = fx.nodata_zero(tmp_path)
    store_path = tmp_path / "store"
    convert(fx.write_manifest(tmp_path / "m.csv", cogs), store_path, chunk_size=16)
    store = chronozarr.open_store(store_path)
    for t, tif in enumerate(export_cog(store, tmp_path / "out", physical=True)):
        with rasterio.open(tif) as src:
            assert src.dtypes == ("float32", "float32")
            values = src.read()
            assert np.array_equal(values, store.physical(t), equal_nan=True)
            assert np.array_equal(np.isnan(values), ~cogs.valid[t])
            expected = cogs.data[t].astype(np.float64) * 0.0001 - 0.1
            assert np.allclose(values[cogs.valid[t]], expected[cogs.valid[t]], rtol=1e-6)
            assert np.isnan(src.nodata)
            assert src.scales == (1.0, 1.0)  # already applied to the values
            assert src.offsets == (0.0, 0.0)
            assert src.units == cogs.units
            assert src.descriptions == cogs.descriptions
            assert np.array_equal(band_validity(src), cogs.valid[t])


def test_physical_export_also_writes_the_mask_of_a_mask_store(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    cogs = fx.scaled_masked(tmp_path)
    store_path = tmp_path / "store"
    convert(fx.write_manifest(tmp_path / "m.csv", cogs), store_path, chunk_size=16)
    for t, tif in enumerate(export_cog(store_path, tmp_path / "out", physical=True)):
        with rasterio.open(tif) as src:
            assert np.array_equal(np.isnan(src.read()), ~cogs.valid[t])
            assert np.array_equal(band_validity(src), cogs.valid[t])
            assert "per_dataset" in {f.name for f in src.mask_flag_enums[0]}


@pytest.mark.skipif(shutil.which("gdalinfo") is None, reason="GDAL command line not installed")
def test_gdalinfo_reads_the_mask_scale_offset_and_unit_of_an_export(tmp_path):
    pytest.importorskip("rasterio")
    cogs = fx.scaled_masked(tmp_path)
    store_path = tmp_path / "store"
    convert(fx.write_manifest(tmp_path / "m.csv", cogs), store_path, chunk_size=16)
    (tif,) = export_cog(store_path, tmp_path / "out", times=[1])
    out = subprocess.run(
        ["gdalinfo", "-json", str(tif)], check=True, capture_output=True, text=True
    )
    first, second = json.loads(out.stdout)["bands"]
    assert first["mask"]["flags"] == ["PER_DATASET"]
    assert "noDataValue" not in first
    assert (first["scale"], first["offset"], first["unit"]) == (0.0001, -0.1, "reflectance")
    assert (second["scale"], second["offset"], second["unit"]) == (0.0002, 0.05, "reflectance")
    assert [first["description"], second["description"]] == ["red", "nir"]
