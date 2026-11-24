"""Coordinate units survive NetCDF/Zarr conversion and comparisons between files."""

from __future__ import annotations

import sys
import warnings

import numpy as np
import pytest
import xarray as xr

from chronozarr import open_store, validate
from chronozarr.convert import PreflightError, convert, plan_conversion

pytestmark = pytest.mark.unit

pytest.importorskip("rasterio")
pyproj = pytest.importorskip("pyproj")


def _source(*, unit="km", declaration="cf", code=5070, day=0):
    crs = pyproj.CRS.from_epsg(code)
    multiplier = 1.0 if unit == "km" else 1000.0
    source = xr.Dataset(
        {"v": (("time", "y", "x"), np.full((1, 2, 2), day + 10, dtype="int16"))},
        coords={
            "time": [np.datetime64("2024-01-01", "ms") + np.timedelta64(day, "D")],
            "x": ("x", np.array([500.0, 501.0]) * multiplier, {"units": unit}),
            "y": ("y", np.array([1001.0, 1000.0]) * multiplier, {"units": unit}),
        },
    )
    if declaration == "cf":
        mapping = crs.to_cf()
        mapping.pop("crs_wkt")
        # CF false offsets use the coordinate variables' units, unlike pyproj's meters.
        for name in ("false_easting", "false_northing"):
            if name in mapping:
                mapping[name] *= multiplier / 1000.0
        source["crs"] = xr.DataArray(0, attrs=mapping)
        source["v"].attrs["grid_mapping"] = "crs"
    elif declaration == "code":
        source.attrs["crs"] = f"EPSG:{code}"
    elif declaration == "wkt":
        source["crs"] = xr.DataArray(0, attrs={"crs_wkt": crs.to_wkt()})
        source["v"].attrs["grid_mapping"] = "crs"
    return source


def _write(source, path, source_format):
    if source_format == "netcdf":
        pytest.importorskip("h5netcdf")
        source.to_netcdf(path, engine="h5netcdf")
    else:
        from zarr.errors import ZarrUserWarning

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ZarrUserWarning)
            source.to_zarr(path, zarr_format=3, consolidated=True)
    return path


@pytest.mark.parametrize("declaration", ["cf", "code", "wkt"])
@pytest.mark.parametrize("source_format", ["netcdf", "zarr"])
def test_kilometer_axes_are_stored_in_the_crs_meter_units(tmp_path, declaration, source_format):
    source = _source(declaration=declaration)
    suffix = "nc" if source_format == "netcdf" else "zarr"
    path = _write(source, tmp_path / f"source.{suffix}", source_format)
    out = tmp_path / "out"

    convert(path, out, variable="v", n_lods=1)

    store = open_store(out)
    assert store.crs.code == "EPSG:5070"
    assert store.levels[0].transform == (1000, 0, 499500, 0, -1000, 1001500)
    np.testing.assert_array_equal(store.to_xarray().x, [500000, 501000])
    np.testing.assert_array_equal(store.to_xarray().y, [1001000, 1000000])
    np.testing.assert_array_equal(store.read(0)[0], source.v.values[0])
    assert validate(out) == []


@pytest.mark.parametrize("code", [32618, 3035])
def test_cf_false_offsets_use_the_declared_coordinate_units(tmp_path, code):
    path = _write(_source(code=code), tmp_path / "offsets.nc", "netcdf")

    plan = plan_conversion(path, variable="v", sample=False)

    assert plan.source.info.grid.crs == f"EPSG:{code}"
    assert plan.source.info.grid.transform == (1000, 0, 499500, 0, -1000, 1001500)


def test_explicit_crs_still_interprets_declared_axis_units(tmp_path):
    path = _write(_source(declaration="none"), tmp_path / "axes.nc", "netcdf")

    plan = plan_conversion(path, variable="v", crs="EPSG:5070", sample=False)

    assert plan.source.info.grid.transform == (1000, 0, 499500, 0, -1000, 1001500)


def test_series_compares_grids_after_converting_axis_units(tmp_path):
    folder = tmp_path / "daily"
    folder.mkdir()
    _write(_source(day=0), folder / "0.nc", "netcdf")
    _write(_source(unit="m", day=1), folder / "1.nc", "netcdf")
    out = tmp_path / "out"

    convert(folder, out, variable="v", n_lods=1)

    store = open_store(out)
    assert store.levels[0].transform == (1000, 0, 499500, 0, -1000, 1001500)
    np.testing.assert_array_equal(store.to_xarray().values[:, 0, 0, 0], [10, 11])


def test_series_member_without_crs_uses_the_first_files_crs_for_axis_units(tmp_path):
    folder = tmp_path / "daily"
    folder.mkdir()
    _write(_source(day=0), folder / "0.nc", "netcdf")
    _write(_source(declaration="none", day=1), folder / "1.nc", "netcdf")

    plan = plan_conversion(folder, variable="v", sample=False)

    assert plan.n_time == 2
    assert plan.source.info.grid.transform == (1000, 0, 499500, 0, -1000, 1001500)


def test_series_member_with_a_conflicting_crs_is_still_refused(tmp_path):
    folder = tmp_path / "daily"
    folder.mkdir()
    _write(_source(day=0), folder / "0.nc", "netcdf")
    _write(_source(declaration="code", code=3857, day=1), folder / "1.nc", "netcdf")

    with pytest.raises(PreflightError, match="declares the CRS"):
        plan_conversion(folder, variable="v", sample=False)


def test_declared_epsg_axis_unit_conversion_needs_no_pyproj(tmp_path, monkeypatch):
    path = _write(_source(declaration="code"), tmp_path / "axes.nc", "netcdf")
    monkeypatch.setitem(sys.modules, "pyproj", None)

    plan = plan_conversion(path, variable="v", sample=False)

    assert plan.source.info.grid.transform == (1000, 0, 499500, 0, -1000, 1001500)


def test_geographic_radian_axes_are_stored_as_degrees(tmp_path):
    source = _source(declaration="none")
    source = source.assign_coords(
        x=("x", np.deg2rad([-74.0, -73.0]), {"units": "radians"}),
        y=("y", np.deg2rad([36.0, 35.0]), {"units": "radians"}),
    )
    path = _write(source, tmp_path / "radians.nc", "netcdf")

    plan = plan_conversion(path, variable="v", crs="EPSG:4326", sample=False)

    np.testing.assert_allclose(plan.source.info.grid.transform, (1, 0, -74.5, 0, -1, 36.5))


@pytest.mark.parametrize("units", [("grad", "gon"), ("degreesE", "degreesN")])
def test_geographic_angular_unit_aliases_are_converted(tmp_path, units):
    multiplier = 200 / 180 if units[0] == "grad" else 1
    source = _source(declaration="none").assign_coords(
        x=("x", np.array([-74.0, -73.0]) * multiplier, {"units": units[0]}),
        y=("y", np.array([36.0, 35.0]) * multiplier, {"units": units[1]}),
    )
    path = _write(source, tmp_path / "angular.nc", "netcdf")

    plan = plan_conversion(path, variable="v", crs="EPSG:4326", sample=False)

    np.testing.assert_allclose(plan.source.info.grid.transform, (1, 0, -74.5, 0, -1, 36.5))


def test_native_grad_axes_remain_in_the_crs_units(tmp_path):
    source = _source(declaration="none").assign_coords(
        x=("x", [1.0, 2.0], {"units": "grad"}),
        y=("y", [48.0, 47.0], {"units": "grad"}),
    )
    path = _write(source, tmp_path / "grad.nc", "netcdf")

    plan = plan_conversion(path, variable="v", crs="EPSG:4807", sample=False)

    assert plan.source.info.grid.transform == (1, 0, 0.5, 0, -1, 48.5)


def test_meter_axes_are_converted_to_a_foot_crs(tmp_path):
    path = _write(_source(unit="m", declaration="code", code=2278), tmp_path / "feet.nc", "netcdf")

    plan = plan_conversion(path, variable="v", sample=False)

    factor = pyproj.CRS.from_epsg(2278).axis_info[0].unit_conversion_factor
    expected = np.array([1000, 0, 499500, 0, -1000, 1001500]) / factor
    np.testing.assert_allclose(plan.source.info.grid.transform, expected)


def test_incompatible_axis_units_are_refused_before_writing(tmp_path):
    source = _source(declaration="code")
    source.x.attrs["units"] = "degrees_east"
    path = _write(source, tmp_path / "wrong.nc", "netcdf")
    out = tmp_path / "out"

    with pytest.raises(ValueError, match=r"coordinate.*units.*incompatible"):
        convert(path, out, variable="v", n_lods=1)

    assert not out.exists()
