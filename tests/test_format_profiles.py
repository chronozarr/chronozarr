"""Dtype profiles, nodata, mask, band metadata and physical values."""

from __future__ import annotations

import numpy as np
import pytest
import xarray as xr
import zarr

import chronozarr
from chronozarr import schema
from chronozarr.schema import Band
from tests.synthetic import CRS, TRANSFORM, make_da, make_truth, reference_reduce

pytestmark = pytest.mark.unit

CS = 8  # tiny cells keep the tests fast; 2 x 2 cells of a 13 x 11 raster


def _encode(tmp_path, truth, **kwargs):
    kwargs.setdefault("chunk_size", CS)
    kwargs.setdefault("anchor_interval", 2)
    bands = kwargs.pop("bands", [f"b{i}" for i in range(truth.shape[1])])
    names = [b if isinstance(b, str) else getattr(b, "name", None) or b["name"] for b in bands]
    da = make_da(truth, names)
    report = chronozarr.encode(da, tmp_path / "s", bands=bands, **kwargs)
    return report, chronozarr.open_store(tmp_path / "s")


def _scene(dtype: str, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    shape = (3, 2, 13, 11)
    if dtype == "float32":
        return rng.normal(10, 50, shape).astype(np.float32)
    info = np.iinfo(dtype)
    return rng.integers(max(info.min, -30000), min(info.max, 30000), shape, dtype=dtype)


# --- dtype profiles ---------------------------------------------------------------------------


@pytest.mark.parametrize("dtype", ["uint8", "uint16", "int16", "float32"])
@pytest.mark.parametrize("shard", [True, False], ids=["sharded", "unsharded"])
def test_every_dtype_roundtrips_with_a_correct_pyramid(tmp_path, dtype, shard):
    truth = _scene(dtype)
    nodata = -9999.0 if dtype == "float32" else None
    truth[0, 0, 0, 0] = nodata if nodata is not None else truth[0, 0, 0, 0]
    report, store = _encode(tmp_path, truth, encoding="none", shard=shard, nodata=nodata)
    assert store.dtype == np.dtype(dtype)
    assert store.attrs.nodata == nodata
    level = truth
    for lod in range(len(store.levels)):
        if lod:
            level, _, _ = reference_reduce(level, nodata=nodata)
        assert np.array_equal(store.to_xarray(lod=lod).values, level), f"{dtype} level {lod}"
    assert chronozarr.validate(tmp_path / "s") == []
    raw = zarr.open_group(str(tmp_path / "s"), mode="r")["0"]["data"]
    assert raw.dtype == np.dtype(dtype)
    assert report.encoding == "none"


@pytest.mark.parametrize("dtype", ["uint8", "uint16"])
def test_unsigned_dtypes_support_star_delta(tmp_path, dtype):
    truth = _scene(dtype)
    _, store = _encode(tmp_path, truth, encoding="star-delta", nodata=0)
    assert store.attrs.temporal.encoding == "star-delta"
    assert np.array_equal(store.to_xarray().values, truth)
    assert zarr.open_group(str(tmp_path / "s"), mode="r")["0"]["data"].dtype == np.dtype(dtype)


@pytest.mark.parametrize("dtype", ["int16", "float32"])
def test_signed_and_float_dtypes_are_none_only(tmp_path, dtype):
    with pytest.raises(ValueError, match="star-delta needs uint8 or uint16 data"):
        chronozarr.encode(
            make_da(_scene(dtype)), tmp_path / "s", chunk_size=CS, encoding="star-delta"
        )
    assert not (tmp_path / "s").exists()
    report, _ = _encode(tmp_path, _scene(dtype), encoding="auto")
    assert report.encoding == "none"
    assert report.selection is None


def test_unsupported_dtype_is_rejected(tmp_path):
    truth = make_truth(2, 1, 8, 8).astype(np.int32)
    with pytest.raises(ValueError, match="unsupported dtype int32"):
        chronozarr.encode(make_da(truth), tmp_path / "s", chunk_size=CS)


# --- nodata -----------------------------------------------------------------------------------


def test_default_nodata_is_zero_for_unsigned_and_null_otherwise(tmp_path):
    _, unsigned = _encode(tmp_path, _scene("uint16"), encoding="none")
    assert unsigned.attrs.nodata == 0
    other = tmp_path / "other"
    other.mkdir()
    _, signed = _encode(other, _scene("int16"), encoding="none")
    assert signed.attrs.nodata is None


def test_explicit_nodata_is_fill_value_attr_and_excluded_from_means(tmp_path):
    truth = np.full((2, 1, 4, 4), 100, dtype=np.uint16)
    truth[:, :, 0, 0] = 65535
    truth[:, :, 2:, 2:] = 65535  # a fully nodata block
    _, store = _encode(tmp_path, truth, chunk_size=4, encoding="none", nodata=65535, n_lods=2)
    data = zarr.open_group(str(tmp_path / "s"), mode="r")["0"]["data"]
    assert data.fill_value == 65535
    assert data.attrs["nodata"] == 65535
    assert store.attrs.nodata == 65535
    coarse = store.read(0, lod=1)[0]
    assert coarse.tolist() == [[100, 100], [100, 65535]]
    assert chronozarr.validate(tmp_path / "s") == []


def test_nodata_null_treats_zero_as_a_value(tmp_path):
    truth = np.zeros((1, 1, 2, 2), dtype=np.uint16)
    truth[0, 0, 0, 0] = 8
    _, store = _encode(tmp_path, truth, chunk_size=2, encoding="none", nodata=None, n_lods=2)
    assert store.read(0, lod=1)[0, 0, 0] == 2  # (8 + 0 + 0 + 0) // 4: zeros count
    assert zarr.open_group(str(tmp_path / "s"), mode="r")["0"]["data"].fill_value == 0


def test_star_delta_with_a_nonzero_nodata_roundtrips(tmp_path):
    truth = make_truth(4, 1, 13, 11)
    truth[:, :, 0, :] = 65535
    _, store = _encode(tmp_path, truth, encoding="star-delta", nodata=65535)
    assert np.array_equal(store.to_xarray().values, truth)
    assert chronozarr.validate(tmp_path / "s") == []


@pytest.mark.parametrize(
    ("dtype", "nodata", "message"),
    [
        ("uint16", 70000, "within uint16 range"),
        ("uint16", -1, "within uint16 range"),
        ("uint8", 1.5, "not an integer"),
        ("uint16", float("nan"), "must be finite"),
        ("float32", float("inf"), "must be finite"),
        ("uint16", "zero", "number or None"),
    ],
)
def test_invalid_nodata_is_rejected(tmp_path, dtype, nodata, message):
    with pytest.raises(ValueError, match=message):
        chronozarr.encode(
            make_da(_scene(dtype)), tmp_path / "s", chunk_size=CS, nodata=nodata, encoding="none"
        )


# --- physical values --------------------------------------------------------------------------


def test_physical_applies_scale_offset_and_nan_for_nodata(tmp_path):
    truth = np.array([[[[0, 1000]], [[5000, 10000]]]], dtype=np.uint16)  # (1 t, 2 b, 1 y, 2 x)
    bands = [
        {"name": "B04", "common_name": "red", "scale": 0.0001},
        {"name": "T", "scale": 0.5, "offset": -10.0, "units": "K"},
    ]
    _, store = _encode(tmp_path, truth, chunk_size=2, bands=bands, encoding="none", n_lods=1)
    physical = store.physical(0)
    assert physical.dtype == np.float32
    assert np.isnan(physical[0, 0, 0])  # stored 0 is nodata
    assert physical[0, 0, 1] == pytest.approx(0.1)
    assert physical[1, 0, 0] == pytest.approx(2490.0)
    assert physical[1, 0, 1] == pytest.approx(4990.0)
    assert np.array_equal(store.read(0), truth[0])  # raw values are untouched
    physical_da = store.to_xarray(physical=True)
    assert physical_da.dtype == np.float32
    assert np.isnan(physical_da.values[0, 0, 0, 0])


def test_physical_uses_the_mask_over_nodata(tmp_path):
    truth = np.array([[[[0, 7]]]], dtype=np.uint16)
    mask = np.array([[[1, 0]]], dtype=np.uint8)  # pixel 0 valid despite value 0; pixel 1 invalid
    _, store = _encode(tmp_path, truth, chunk_size=2, mask=mask, encoding="none", n_lods=1)
    physical = store.physical(0)
    assert physical[0, 0, 0] == 0.0
    assert np.isnan(physical[0, 0, 1])


def test_physical_defaults_to_identity_scale(tmp_path):
    truth = _scene("int16")
    _, store = _encode(tmp_path, truth, encoding="none")
    assert np.array_equal(store.physical(1, lod=0), truth[1].astype(np.float32))


# --- mask -------------------------------------------------------------------------------------


def _masked_scene():
    truth = make_truth(3, 2, 13, 11)
    truth[truth == 0] = 1  # no nodata-valued pixels: validity comes from the mask alone
    rng = np.random.default_rng(5)
    mask = (rng.random((3, 13, 11)) > 0.35).astype(np.uint8)
    mask[:, 8:, 8:] = 0  # a corner with no valid pixels at any timestep
    return truth, mask


@pytest.mark.parametrize("shard", [True, False], ids=["sharded", "unsharded"])
def test_mask_is_stored_reduced_and_drives_the_data_means(tmp_path, shard):
    truth, mask = _masked_scene()
    _, store = _encode(tmp_path, truth, mask=mask, shard=shard, encoding="star-delta")
    assert store.attrs.mask_variable == "mask"
    level, level_mask = truth, mask
    for lod in range(len(store.levels)):
        if lod:
            level, level_mask, _ = reference_reduce(level, nodata=0, mask=level_mask)
        assert np.array_equal(store.to_xarray(lod=lod).values, level), f"data level {lod}"
        for t in range(3):
            assert np.array_equal(store.read_mask(t, lod), level_mask[t]), f"mask {lod} t={t}"
    assert chronozarr.validate(tmp_path / "s") == []
    assert store.read_coverage(0) is None


def test_mask_layout_matches_the_data_array(tmp_path):
    truth, mask = _masked_scene()
    _encode(tmp_path, truth, mask=mask, shard=True, shard_time=2)
    root = zarr.open_group(str(tmp_path / "s"), mode="r")
    for lod in ("0", "1"):
        data, plane = root[lod]["data"], root[lod]["mask"]
        assert plane.dtype == np.uint8
        assert plane.metadata.dimension_names == ("time", "y", "x")
        assert plane.attrs["_ARRAY_DIMENSIONS"] == ["time", "y", "x"]
        assert plane.chunks == (1, CS, CS)
        assert plane.shards == (2, CS, CS)
        assert plane.shape == (3, *data.shape[2:])
    attrs = root.attrs["chronozarr"]
    assert attrs["mask_variable"] == "mask"
    assert "coverage_variable" not in attrs


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.astype(np.int16), "mask must be uint8"),
        (lambda m: m * 2, "only 0 .* and 1"),
        (lambda m: m[:, :5], "differs from the data"),
    ],
)
def test_bad_mask_is_rejected(tmp_path, mutate, message):
    truth, mask = _masked_scene()
    with pytest.raises(ValueError, match=message):
        chronozarr.encode(
            make_da(truth),
            tmp_path / "s",
            chunk_size=CS,
            mask=xr.DataArray(mutate(mask), dims=("time", "y", "x")),
        )
    assert not (tmp_path / "s").exists()


def test_bool_mask_is_accepted(tmp_path):
    truth, mask = _masked_scene()
    _, store = _encode(tmp_path, truth, mask=mask.astype(bool), encoding="none")
    assert np.array_equal(store.read_mask(1), mask[1])


# --- band metadata ----------------------------------------------------------------------------


def test_bands_are_objects_with_a_names_mirror(tmp_path):
    bands = [
        Band("B04", common_name="red", scale=0.0001, offset=0.0, units="reflectance"),
        Band("B08", common_name="nir"),
    ]
    _, store = _encode(tmp_path, make_truth(2, 2, 13, 11), bands=bands, encoding="none")
    block = zarr.open_group(str(tmp_path / "s"), mode="r").attrs["chronozarr"]
    assert block["bands"] == [
        {
            "name": "B04",
            "common_name": "red",
            "scale": 0.0001,
            "offset": 0.0,
            "units": "reflectance",
        },
        {"name": "B08", "common_name": "nir"},
    ]
    assert block["band_names"] == ["B04", "B08"]
    assert store.bands == ("B04", "B08")
    assert store.attrs.bands == tuple(bands)
    assert list(zarr.open_group(str(tmp_path / "s"), mode="r")["0"]["band"][:]) == ["B04", "B08"]


def test_band_names_come_from_the_coordinate_by_default(tmp_path):
    truth = make_truth(2, 2, 13, 11)
    chronozarr.encode(make_da(truth, ["red", "green"]), tmp_path / "s", chunk_size=CS)
    assert chronozarr.open_store(tmp_path / "s").bands == ("red", "green")


@pytest.mark.parametrize(
    ("bands", "message"),
    [
        (["a", "b", "c"], "3 band names for 2 bands"),
        (["x", "y"], "differ from the band coordinate"),
        ([{"name": "a", "scale": "big"}, "b"], "expected a finite number"),
        ([{"common_name": "red"}, "b"], "missing required key 'name'"),
    ],
)
def test_bad_bands_are_rejected(tmp_path, bands, message):
    with pytest.raises(ValueError, match=message):
        chronozarr.encode(
            make_da(make_truth(2, 2, 8, 8), ["a", "b"]), tmp_path / "s", chunk_size=CS, bands=bands
        )


def test_band_mismatch_between_attrs_and_band_names_is_flagged(tmp_path):
    _encode(tmp_path, make_truth(2, 2, 13, 11), encoding="none")
    root = zarr.open_group(str(tmp_path / "s"), mode="r+", use_consolidated=False)
    block = dict(root.attrs["chronozarr"])
    block["band_names"] = ["wrong", "names"]
    root.attrs["chronozarr"] = block
    assert any("band_names" in p for p in chronozarr.validate(tmp_path / "s"))


def test_iterable_bands_need_names(tmp_path):
    steps = iter(make_truth(2, 1, 8, 8))
    with pytest.raises(ValueError, match="no band names"):
        chronozarr.encode(
            steps,
            tmp_path / "s",
            times=make_da(make_truth(2, 1, 8, 8)).time.values,
            crs=CRS,
            transform=TRANSFORM,
            chunk_size=CS,
        )


def test_band_schema_accepts_names_and_objects():
    assert schema.parse_bands(["a", "b"], "bands") == (Band("a"), Band("b"))
    with pytest.raises(schema.SchemaError, match="must be unique"):
        schema.parse_bands(["a", {"name": "a"}], "bands")
