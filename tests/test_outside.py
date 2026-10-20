"""Stores that chronozarr did not write decode, with every assumption reported.

The fixtures are shaped like ndpyramid 0.4 output (coarsened and reprojected pyramids with the
legacy `datasets` list and no `proj:` or `spatial:` attributes) and like the `reflectance` group of
an ESA EOPF Sentinel-2 product (multiscales v1 layout, one 2-D array per band, no time axis). The
values read through chronozarr are compared with the arrays zarr-python reads.
"""

from __future__ import annotations

import json
import shutil
import warnings
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import xarray as xr
import zarr
from click.testing import CliRunner
from zarr.errors import ZarrUserWarning

import chronozarr
from chronozarr import schema
from chronozarr.cli import main
from chronozarr.stac import build_stac
from tests.narrow import array_at, attrs_block, group_at
from tests.outside_stores import (
    EOPF_BANDS,
    EOPF_LEVEL_BANDS,
    EOPF_ORIGIN,
    MERCATOR_EXTENT,
    TIMES,
    Written,
    fill_value_attr,
    write_eopf_reflectance,
    write_ndpyramid_coarsen,
    write_ndpyramid_reproject,
)
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit


def has(texts: tuple[str, ...] | list[str], *fragments: str) -> bool:
    return any(all(f in text for f in fragments) for text in texts)


@pytest.fixture(scope="module")
def coarsen(tmp_path_factory) -> Written:
    return write_ndpyramid_coarsen(tmp_path_factory.mktemp("ndp") / "coarsen.zarr")


@pytest.fixture(scope="module")
def reproject(tmp_path_factory) -> Written:
    return write_ndpyramid_reproject(tmp_path_factory.mktemp("ndp") / "reproject.zarr")


@pytest.fixture(scope="module")
def eopf(tmp_path_factory) -> Written:
    return write_eopf_reflectance(tmp_path_factory.mktemp("eopf") / "reflectance.zarr")


@pytest.fixture(scope="module")
def eopf_encoded(tmp_path_factory) -> Written:
    path = tmp_path_factory.mktemp("eopf") / "encoded.zarr"
    return write_eopf_reflectance(path, encoded=True)


def zarr_values(path: Path, member: str) -> np.ndarray:
    """What zarr-python reads, with no chronozarr code in between."""
    node = zarr.open_group(str(path), mode="r", zarr_format=3)[member]
    assert isinstance(node, zarr.Array)
    return np.asarray(node[:])


def consolidate(path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ZarrUserWarning)  # consolidated metadata is deliberate
        zarr.consolidate_metadata(str(path))


# --- ndpyramid, coarsened ---------------------------------------------------------------------


def test_a_coarsened_ndpyramid_opens_finest_level_first(coarsen):
    store = chronozarr.open_store(coarsen.path)
    assert [level.path for level in store.levels] == ["0", "1", "2"]
    assert [level.shape for level in store.levels] == [
        (3, 1, 48, 64),
        (3, 1, 24, 32),
        (3, 1, 12, 16),
    ]
    assert store.bands == ("tmax",)
    assert store.attrs.variable == "tmax"
    np.testing.assert_allclose(store.levels[0].transform, (5.625, 0, -180.0, 0, -5.625, 90.0))
    np.testing.assert_allclose(store.levels[1].transform, (11.25, 0, -180.0, 0, -11.25, 90.0))
    assert store.levels[1].resolution == pytest.approx(11.25)
    assert store.crs.epsg == 4326


def test_a_coarsened_ndpyramid_reads_the_stored_values(coarsen):
    store = chronozarr.open_store(coarsen.path)
    for level in range(3):
        expected = coarsen.levels[str(level)]
        assert store.levels[level].data.dtype == np.float32
        for t in range(3):
            np.testing.assert_array_equal(store.read(t, level)[0], expected[t])
        np.testing.assert_array_equal(
            zarr_values(coarsen.path, f"{level}/tmax"), store.to_xarray(level).values[:, 0]
        )


def test_the_times_come_from_the_cf_units_of_the_time_array(coarsen):
    store = chronozarr.open_store(coarsen.path)
    np.testing.assert_array_equal(store.times, TIMES.astype("datetime64[ms]"))
    assert store.attrs.times[0] == "2020-01-01T00:00:00Z"


def test_declared_scale_and_offset_are_applied_and_not_called_assumed(coarsen):
    store = chronozarr.open_store(coarsen.path)
    assert (store.attrs.bands[0].scale, store.attrs.bands[0].offset) == (1.0, 0.0)
    assert not has(store.notes, "scale and offset")
    np.testing.assert_array_equal(
        store.physical(1, 0)[0], coarsen.levels["0"][1].astype("float32") * 1.0
    )


def test_a_nan_fill_value_marks_missing_pixels_without_a_sentinel(coarsen):
    store = chronozarr.open_store(coarsen.path)
    assert store.nodata is None
    assert np.isnan(store.physical(0, 0)[0, :3, :5]).all()
    assert not np.isnan(store.physical(0, 0)[0, 10:, 10:]).any()


def test_the_notes_say_what_was_read_from_where(coarsen):
    store = chronozarr.open_store(coarsen.path)
    assert has(store.notes, "no chronozarr block", "multiscales")
    assert has(store.notes, "geometry", "x and y coordinate arrays")
    assert has(store.notes, "CRS", "spatial_ref")
    assert has(store.notes, "overview", "writer")


def test_the_xarray_engine_opens_a_coarsened_ndpyramid(coarsen):
    dataset = xr.open_dataset(coarsen.path, engine="chronozarr")
    assert dataset["tmax"].dims == ("time", "band", "y", "x")
    assert dataset["tmax"].shape == (3, 1, 48, 64)
    np.testing.assert_array_equal(dataset["tmax"].values[:, 0], coarsen.levels["0"])
    np.testing.assert_array_equal(dataset["time"].values, TIMES)
    assert has(dataset.attrs["chronozarr_notes"], "no chronozarr block")
    coarse = xr.open_dataset(coarsen.path, engine="chronozarr", lod=2)
    np.testing.assert_array_equal(coarse["tmax"].values[:, 0], coarsen.levels["2"])


def test_dask_chunks_follow_the_stored_chunks_of_a_foreign_store(reproject):
    pytest.importorskip("dask")
    dataset = xr.open_dataset(reproject.path, engine="chronozarr", chunks={})
    chunks = dataset["tmax"].chunks
    assert chunks is not None
    assert chunks[0] == (3,)
    assert chunks[2] == (16, 16, 16, 16)


def test_info_names_a_store_without_a_block(coarsen):
    result = CliRunner().invoke(main, ["info", str(coarsen.path)])
    assert result.exit_code == 0, result.output
    assert "version:   not a chronozarr store" in result.stdout
    assert "bands:     tmax" in result.stdout
    assert "crs:       EPSG:4326" in result.stdout
    assert "times:     3 (2020-01-01T00:00:00Z .. 2020-03-01T00:00:00Z)" in result.stdout
    assert any(line.startswith("note: no chronozarr block") for line in result.stdout.splitlines())


def test_validate_says_what_is_missing_and_that_the_store_decodes(coarsen):
    problems = chronozarr.validate(coarsen.path)
    assert any("missing required key 'chronozarr'" in p for p in problems)
    result = CliRunner().invoke(main, ["validate", str(coarsen.path)])
    assert result.exit_code == 1
    assert "missing required key 'chronozarr'" in result.stderr
    assert "note: decodes: 3 levels, 1 band, 3 timesteps" in result.stderr
    assert "no chronozarr block" in result.stderr


# --- ndpyramid, reprojected -------------------------------------------------------------------


def test_a_reprojected_pyramid_that_runs_coarse_to_fine_is_read_finest_first(reproject):
    store = chronozarr.open_store(reproject.path)
    assert [level.path for level in store.levels] == ["2", "1", "0"]
    assert [level.shape[2:] for level in store.levels] == [(64, 64), (32, 32), (16, 16)]
    np.testing.assert_allclose(
        store.levels[0].transform,
        (
            2 * MERCATOR_EXTENT / 64,
            0,
            -MERCATOR_EXTENT,
            0,
            -2 * MERCATOR_EXTENT / 64,
            MERCATOR_EXTENT,
        ),
        rtol=1e-6,
    )
    assert has(store.notes, "coarse to fine")
    assert store.crs.code == "EPSG:3857"
    assert has(store.notes, "CRS", "layout")


def test_a_reprojected_pyramid_reads_time_spanning_chunks(reproject):
    store = chronozarr.open_store(reproject.path)
    assert store.levels[0].data.chunks == (3, 16, 16)
    np.testing.assert_array_equal(store.read(2, 0)[0], reproject.levels["2"][2])
    np.testing.assert_array_equal(store.read(1, 2)[0], reproject.levels["0"][1])
    cell = store.read_cell(2, 1, 2, lod=0)
    np.testing.assert_array_equal(cell[0], reproject.levels["2"][2, 16:32, 32:48])
    assert store.levels[0].cell_shape == (16, 16)
    assert store.levels[0].grid == (4, 4)


def test_int32_time_decodes_from_cf_days(reproject):
    store = chronozarr.open_store(reproject.path)
    np.testing.assert_array_equal(store.times, TIMES.astype("datetime64[ms]"))


# --- EOPF reflectance -------------------------------------------------------------------------


def test_eopf_per_band_arrays_stack_on_a_band_axis_in_spectral_order(eopf):
    store = chronozarr.open_store(eopf.path)
    assert store.bands == EOPF_BANDS
    assert store.attrs.variable == "data"
    level = store.levels[0]
    assert level.path == "r10m"
    assert level.shape == (1, 3, 120, 120)
    assert level.transform == (10.0, 0.0, EOPF_ORIGIN[0], 0.0, -10.0, EOPF_ORIGIN[1])
    assert store.crs.code == "EPSG:32633"
    for band, name in enumerate(EOPF_BANDS):
        np.testing.assert_array_equal(
            store.read(0, 0)[band], zarr_values(eopf.path, f"r10m/{name}")
        )
        np.testing.assert_array_equal(store.read(0, 1)[band], eopf.levels["r20m"][band])


def test_a_band_that_not_every_level_has_is_left_out_and_named(eopf):
    store = chronozarr.open_store(eopf.path)
    assert store.bands == EOPF_BANDS
    assert all(level.shape[1] == 3 for level in store.levels)
    assert has(store.notes, "bands that not every level has", "b08 at r10m", "b8a, b11 at r20m")
    assert has(store.notes, "b01, b8a, b11 at r60m")


def test_levels_that_share_no_band_are_refused(eopf_copy):
    for band in ("b02", "b03", "b04"):
        shutil.rmtree(eopf_copy / "r60m" / band)
    consolidate(eopf_copy)
    with pytest.raises(schema.SchemaError, match="no band is present at every level"):
        chronozarr.open_store(eopf_copy)


def test_a_store_with_no_time_axis_reads_as_one_dated_timestep(eopf):
    store = chronozarr.open_store(eopf.path)
    assert len(store.times) == 1
    assert has(store.notes, "no time axis", "one timestep", "1970-01-01")
    assert store.to_xarray().sizes["time"] == 1
    with pytest.raises(IndexError, match="timestep 1 out of range"):
        store.read(1, 0)


def test_eopf_scale_is_not_applied_to_values_that_are_decoded_already(eopf):
    """The public product stores float32 reflectance under `_eopf_attrs` for the `<u2` counts."""
    store = chronozarr.open_store(eopf.path)
    assert [(b.scale, b.offset) for b in store.attrs.bands] == [(None, None)] * 3
    assert has(store.notes, "_eopf_attrs scale_factor and add_offset are not applied", "float32")
    stored = store.read(0, 0)
    physical = store.physical(0, 0)
    valid = stored != 0.0
    np.testing.assert_array_equal(physical[valid], stored[valid])


def test_eopf_scale_applies_to_values_that_are_still_encoded(eopf_encoded):
    store = chronozarr.open_store(eopf_encoded.path)
    assert [(b.scale, b.offset) for b in store.attrs.bands] == [(0.0001, -0.1)] * 3
    assert not has(store.notes, "scale and offset are not declared")
    assert not has(store.notes, "are not applied")
    counts = store.read(0, 0)
    assert counts.dtype == np.uint16
    physical = store.physical(0, 0)
    valid = counts > 0
    np.testing.assert_allclose(
        physical[valid], counts[valid].astype("float64") * 0.0001 - 0.1, atol=1e-7
    )
    assert np.isnan(physical[~valid]).all()


def test_each_level_keeps_its_own_missing_value_marker(eopf):
    store = chronozarr.open_store(eopf.path)
    assert store.nodata == 0.0
    assert [level.nodata for level in store.levels] == [0.0, 0.0, None]
    assert has(
        store.notes,
        "mark missing pixels differently",
        "0 at r10m, r20m",
        "NaN or no marker at r60m",
    )
    assert np.isnan(store.physical(0, 0)[:, :4, :6]).all()
    assert not np.isnan(store.physical(0, 0)[:, 10:, 10:]).any()
    coarse = store.physical(0, 2)
    assert np.isnan(coarse[:, :4, :6]).all()
    assert np.isfinite(coarse[:, 10:, 10:]).all()


def test_levels_that_are_not_a_factor_two_ladder_are_listed(eopf):
    store = chronozarr.open_store(eopf.path)
    assert [level.path for level in store.levels] == ["r10m", "r20m", "r60m"]
    assert has(store.notes, "r60m (6x)")
    assert not has(store.notes, "r20m (2x)")


def test_a_level_is_read_by_its_own_cells(eopf):
    store = chronozarr.open_store(eopf.path)
    level = store.levels[1]
    assert level.cell_shape == (30, 30)
    cell = store.read_cell(0, 1, 1, lod=1)
    np.testing.assert_array_equal(cell, eopf.levels["r20m"][:, 30:60, 30:60])


def test_the_registrations_of_another_writer_are_not_checked_for_a_foreign_store(eopf):
    store = chronozarr.open_store(eopf.path)
    assert not has(store.notes, "registration")


def test_the_xarray_engine_applies_a_declared_scale_to_encoded_values(eopf_encoded):
    dataset = xr.open_dataset(eopf_encoded.path, engine="chronozarr")
    raw = xr.open_dataset(eopf_encoded.path, engine="chronozarr", physical=False)
    counts = eopf_encoded.levels["r10m"]
    np.testing.assert_array_equal(raw["data"].values[0], counts)
    expected = np.where(counts > 0, counts.astype("float32") * 0.0001 - 0.1, np.nan)
    np.testing.assert_allclose(dataset["data"].values[0], expected, atol=1e-7, equal_nan=True)
    assert list(dataset["band"].values) == list(EOPF_BANDS)
    assert raw.attrs["nodata"] == 0


# --- Assumptions ------------------------------------------------------------------------------


@pytest.fixture
def eopf_copy(eopf, tmp_path):
    copied = tmp_path / "copy.zarr"
    shutil.copytree(eopf.path, copied)
    return copied


def edit_arrays(path: Path, edit) -> None:
    root = zarr.open_group(str(path), mode="r+", zarr_format=3, use_consolidated=False)
    for asset in ("r10m", "r20m", "r60m"):
        group = group_at(root, asset)
        for band in EOPF_BANDS:
            array = array_at(group, band)
            attrs = array.attrs.asdict()
            edit(attrs)
            array.attrs.put(attrs)
    consolidate(path)


def test_scale_and_offset_that_are_not_declared_are_one_and_zero_and_flagged(eopf_copy, eopf):
    edit_arrays(eopf_copy, lambda a: a.pop("_eopf_attrs"))
    store = chronozarr.open_store(eopf_copy)
    assert [(b.scale, b.offset) for b in store.attrs.bands] == [(None, None)] * 3
    assert has(store.notes, "scale and offset are not declared", "stored values")
    np.testing.assert_array_equal(
        store.physical(0, 0)[0][10:, 10:], eopf.levels["r10m"][0][10:, 10:]
    )


def test_cf_scale_factor_on_an_array_is_read_too(eopf_copy):
    def cf(attrs: dict[str, Any]) -> None:
        attrs.pop("_eopf_attrs")
        attrs.update(scale_factor=0.5, add_offset=2.0)

    edit_arrays(eopf_copy, cf)
    store = chronozarr.open_store(eopf_copy)
    assert [(b.scale, b.offset) for b in store.attrs.bands] == [(0.5, 2.0)] * 3
    assert not has(store.notes, "scale and offset are not declared")


def test_without_a_fill_value_attribute_the_zero_fill_value_is_not_nodata(eopf_copy):
    edit_arrays(eopf_copy, lambda a: a.pop("_FillValue"))
    store = chronozarr.open_store(eopf_copy)
    assert store.nodata is None
    assert has(store.notes, "no _FillValue", "valid")
    assert not np.isnan(store.physical(0, 0)[:, :4, :6]).any()


def test_a_fill_value_set_on_the_array_is_taken_as_nodata_and_flagged(tmp_path):
    path = tmp_path / "ints.zarr"
    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    root.attrs["multiscales"] = {"layout": [{"asset": "a"}]}
    group = root.create_group("a")
    group.attrs.update(
        {
            "proj:code": "EPSG:32633",
            "spatial:transform": [10.0, 0.0, 0.0, 0.0, -10.0, 80.0],
            "spatial:dimensions": ["y", "x"],
        }
    )
    values = np.arange(64, dtype="int16").reshape(8, 8)
    values[0, 0] = -9999
    array = group.create_array(
        "b1", shape=(8, 8), dtype="int16", fill_value=-9999, dimension_names=("y", "x")
    )
    array[:] = values
    consolidate(path)
    store = chronozarr.open_store(path)
    assert store.nodata == -9999
    assert has(store.notes, "fill_value", "-9999")
    assert np.isnan(store.physical(0, 0)[0, 0, 0])
    assert store.physical(0, 0)[0, 0, 1] == 1.0


def test_an_integer_fill_value_attribute_is_decoded(tmp_path):
    path = tmp_path / "ints.zarr"
    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    root.attrs["multiscales"] = {"layout": [{"asset": "a"}]}
    group = root.create_group("a")
    group.attrs.update(
        {
            "proj:code": "EPSG:32633",
            "spatial:transform": [10.0, 0.0, 0.0, 0.0, -10.0, 80.0],
        }
    )
    array = group.create_array(
        "b1",
        shape=(8, 8),
        dtype="int16",
        dimension_names=("y", "x"),
        attributes={"_FillValue": fill_value_attr(-32768, "i")},
    )
    array[:] = np.full((8, 8), -32768, dtype="int16")
    consolidate(path)
    store = chronozarr.open_store(path)
    assert store.nodata == -32768
    assert np.isnan(store.physical(0, 0)).all()


def test_registered_proj_and_spatial_attributes_win_over_other_sources(eopf_copy):
    root = zarr.open_group(str(eopf_copy), mode="r+", zarr_format=3, use_consolidated=False)
    for asset in ("r10m", "r20m", "r60m"):
        group = group_at(root, asset)
        attrs = group.attrs.asdict()
        attrs["proj:code"] = "EPSG:32632"
        group.attrs.put(attrs)
    consolidate(eopf_copy)
    assert chronozarr.open_store(eopf_copy).crs.code == "EPSG:32632"


def test_spatial_registration_node_shifts_the_origin_half_a_pixel(eopf_copy):
    root = zarr.open_group(str(eopf_copy), mode="r+", zarr_format=3, use_consolidated=False)
    for asset in ("r10m", "r20m", "r60m"):
        group = group_at(root, asset)
        attrs = group.attrs.asdict()
        attrs["spatial:registration"] = "node"
        group.attrs.put(attrs)
    layout = attrs_block(root, "multiscales")
    for entry in layout["layout"]:
        entry.pop("spatial:transform")
    root.attrs["multiscales"] = layout
    consolidate(eopf_copy)
    store = chronozarr.open_store(eopf_copy)
    assert store.levels[0].transform[2] == EOPF_ORIGIN[0] - 5.0
    assert store.levels[0].transform[5] == EOPF_ORIGIN[1] + 5.0
    assert has(store.notes, "node")


def test_a_crs_in_a_wkt1_grid_mapping_is_read_without_pyproj_by_its_authority(
    coarsen, monkeypatch
):
    monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    assert chronozarr.open_store(coarsen.path).crs.code == "EPSG:4326"


# --- What is refused --------------------------------------------------------------------------


def test_a_group_with_no_multiscales_is_refused_naming_what_is_needed(tmp_path):
    path = tmp_path / "plain.zarr"
    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    root.create_array("a", shape=(4, 4), dtype="float32", dimension_names=("y", "x"))
    with pytest.raises(schema.SchemaError, match="no chronozarr block and no multiscales layout"):
        chronozarr.open_store(path)
    result = CliRunner().invoke(main, ["validate", str(path)])
    assert result.exit_code == 1
    assert "note: does not decode: " in result.stderr


def test_a_rotated_transform_is_refused(eopf_copy):
    root = zarr.open_group(str(eopf_copy), mode="r+", zarr_format=3, use_consolidated=False)
    layout = attrs_block(root, "multiscales")
    for entry in layout["layout"]:
        entry["spatial:transform"][1] = 0.5
    root.attrs["multiscales"] = layout
    for asset in ("r10m", "r20m", "r60m"):
        group = group_at(root, asset)
        attrs: dict[str, Any] = dict(group.attrs.asdict())
        attrs["spatial:transform"][1] = 0.5
        group.attrs.put(attrs)
    consolidate(eopf_copy)
    with pytest.raises(schema.SchemaError, match="north-up"):
        chronozarr.open_store(eopf_copy)


def test_a_store_with_no_crs_anywhere_is_refused_naming_the_options(eopf_copy):
    root = zarr.open_group(str(eopf_copy), mode="r+", zarr_format=3, use_consolidated=False)
    for node in (root, *(group_at(root, a) for a in ("r10m", "r20m", "r60m"))):
        attrs = node.attrs.asdict()
        attrs.pop("proj:code", None)
        node.attrs.put(attrs)
    for asset in ("r10m", "r20m", "r60m"):
        array_at(group_at(root, asset), "spatial_ref").attrs.put({})
    consolidate(eopf_copy)
    with pytest.raises(schema.SchemaError, match=r"no CRS.*proj:code.*proj:wkt2"):
        chronozarr.open_store(eopf_copy)


def test_arrays_without_dimension_names_are_refused(eopf_copy):
    for band in EOPF_LEVEL_BANDS["r10m"]:
        path = eopf_copy / "r10m" / band / "zarr.json"
        document = json.loads(path.read_text())
        document.pop("dimension_names")
        path.write_text(json.dumps(document))
    consolidate(eopf_copy)
    with pytest.raises(schema.SchemaError, match="dimension names"):
        chronozarr.open_store(eopf_copy)


def test_a_dtype_chronozarr_does_not_read_is_refused_by_name(tmp_path):
    path = tmp_path / "doubles.zarr"
    root = zarr.open_group(str(path), mode="w", zarr_format=3)
    root.attrs["multiscales"] = {"layout": [{"asset": "a"}]}
    group = root.create_group("a")
    group.attrs.update(
        {"proj:code": "EPSG:32633", "spatial:transform": [10.0, 0.0, 0.0, 0.0, -10.0, 80.0]}
    )
    group.create_array("b1", shape=(8, 8), dtype="float64", dimension_names=("y", "x"))
    consolidate(path)
    with pytest.raises(schema.SchemaError, match=r"float64.*uint8, uint16, int16 or float32"):
        chronozarr.open_store(path)


def test_levels_with_different_times_are_refused(coarsen, tmp_path):
    copied = tmp_path / "copy.zarr"
    shutil.copytree(coarsen.path, copied)
    root = zarr.open_group(str(copied), mode="r+", zarr_format=3, use_consolidated=False)
    array_at(group_at(root, "1"), "time")[:] = np.array([0, 31, 61], dtype="int64")
    consolidate(copied)
    with pytest.raises(schema.SchemaError, match="levels 0 and 1 have different times"):
        chronozarr.open_store(copied)


def test_a_time_dimension_without_a_coordinate_array_is_refused(coarsen, tmp_path):
    copied = tmp_path / "copy.zarr"
    shutil.copytree(coarsen.path, copied)
    for level in "012":
        shutil.rmtree(copied / level / "time")
    consolidate(copied)
    with pytest.raises(schema.SchemaError, match="'time' has no coordinate array"):
        chronozarr.open_store(copied)


def test_a_calendar_that_is_not_gregorian_is_refused_with_the_converter_named(coarsen, tmp_path):
    copied = tmp_path / "copy.zarr"
    shutil.copytree(coarsen.path, copied)
    root = zarr.open_group(str(copied), mode="r+", zarr_format=3, use_consolidated=False)
    for level in "012":
        array_at(group_at(root, level), "time").attrs["calendar"] = "360_day"
    consolidate(copied)
    with pytest.raises(schema.SchemaError, match=r"360_day.*chronozarr convert"):
        chronozarr.open_store(copied)


def test_stac_describes_only_chronozarr_stores(coarsen, tmp_path):
    with pytest.raises(ValueError, match="no chronozarr block"):
        build_stac(chronozarr.open_store(coarsen.path), href="https://example.org/s", id="s")


# --- Any chunking decodes ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def native(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("native") / "store"
    truth = make_truth(3, 2, 64, 64)
    mask = np.ones((3, 64, 64), dtype="uint8")
    mask[:, :8, :8] = 0
    build_store(path, truth, shard=False, chunk_size=16, mask=mask, coverage=mask * 2, nodata=None)
    return path


def rechunk(path: Path, chunks: tuple[int, int, int, int]) -> None:
    """Rewrite the data arrays with other chunks, keeping values, attributes and codecs."""
    root = zarr.open_group(str(path), mode="r+", zarr_format=3, use_consolidated=False)
    for level in ("0", "1", "2"):
        group = group_at(root, level)
        old = array_at(group, "data")
        values, attrs = np.asarray(old[:]), old.attrs.asdict()
        shutil.rmtree(path / level / "data")
        new = zarr.create_array(
            group.store,
            name=f"{level}/data",
            shape=values.shape,
            chunks=chunks,
            dtype=values.dtype,
            fill_value=0,
            dimension_names=("time", "band", "y", "x"),
            attributes=attrs,
        )
        new[:] = values
    consolidate(path)


@pytest.mark.parametrize(
    ("chunks", "cell_shape", "note"),
    [
        ((3, 2, 16, 16), (16, 16), "chunks must be (1, 2, 16, 16), got (3, 2, 16, 16)"),
        ((1, 1, 16, 16), (16, 16), "chunks must be (1, 2, 16, 16), got (1, 1, 16, 16)"),
        ((1, 2, 16, 8), (16, 8), "chunks must be (1, 2, 16, 16), got (1, 2, 16, 8)"),
        ((1, 2, 15, 15), (15, 15), "cell size must be positive and even"),
    ],
    ids=["time-spanning", "one-band-per-chunk", "non-square", "odd"],
)
def test_a_chronozarr_store_with_other_chunks_decodes_exactly(
    native, tmp_path, chunks, cell_shape, note
):
    copied = tmp_path / "copy"
    shutil.copytree(native, copied)
    rechunk(copied, chunks)
    opened = chronozarr.open_store(copied)
    truth = chronozarr.open_store(native)
    assert opened.levels[0].cell_shape == cell_shape
    assert opened.levels[0].grid == (-(-64 // cell_shape[0]), -(-64 // cell_shape[1]))
    for level in range(3):
        for t in range(3):
            np.testing.assert_array_equal(opened.read(t, level), truth.read(t, level))
    rows, cols = opened.levels[0].grid
    np.testing.assert_array_equal(
        opened.read_cell(1, rows - 1, cols - 1, 0)[:, -1, -1], truth.read(1, 0)[:, -1, -1]
    )
    assert has(opened.notes, note)
    assert any(note in p for p in chronozarr.validate(copied))
    ds = xr.open_dataset(copied, engine="chronozarr", physical=False)
    np.testing.assert_array_equal(ds["data"].values, truth.to_xarray().values)
