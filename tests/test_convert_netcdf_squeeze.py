"""`convert` on NetCDF variables with a dimension of length 1 besides time, y and x.

OISST keeps a `zlev` of length 1 on every variable, other products a `height` or a `level`. It is
squeezed, and the plan names it and the value it held. A longer one is not guessed at.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import xarray as xr
from click.testing import CliRunner

import chronozarr
from chronozarr.cli import main
from chronozarr.convert import convert, plan_conversion
from tests.fixtures.netcdf_days import HEIGHT, WIDTH, day_values, lat, lon, write_days

pytestmark = pytest.mark.unit

pytest.importorskip("rasterio")
pytest.importorskip("h5netcdf")

OPTIONS: dict[str, Any] = {"chunk_size": 16}


def _stored(out) -> np.ndarray:
    return chronozarr.open_store(out).to_xarray().values


def _write(path, dims: tuple[str, ...], values: np.ndarray, **coords: Any) -> None:
    """A file with `sst` over `dims` and the extra coordinates, on the grid of the fixtures."""
    xr.Dataset(
        {"sst": (dims, values, {"scale_factor": 0.01})},
        coords={
            "time": ("time", [0.0], {"units": "days since 2020-01-01"}),
            "lat": ("lat", lat()),
            "lon": ("lon", lon()),
            **coords,
        },
    ).to_netcdf(path, engine="h5netcdf")


def test_a_single_file_with_a_length_one_dimension_converts(tmp_path):
    source = write_days(tmp_path / "one.nc", [0, 1, 2])
    out = tmp_path / "store"
    report = convert(source, out, variable="sst", crs="EPSG:4326", **OPTIONS)
    assert report.plan.source.kind == "NetCDF file"
    assert any(n.startswith("squeezed:") and "zlev" in n for n in report.plan.source.notes)
    # North-up rows, the packed integers as they are in the file.
    expected = np.stack([day_values(d)[::-1] for d in range(3)])[:, np.newaxis]
    assert np.array_equal(_stored(out), expected)


def test_the_squeezed_dimension_is_reported_with_its_value_and_units(tmp_path):
    source = write_days(tmp_path / "one.nc", [0])
    plan = plan_conversion(source, variable="sst", crs="EPSG:4326", sample=False)
    (note,) = (n for n in plan.source.notes if n.startswith("squeezed:"))
    assert note == "squeezed:   zlev (length 1, value 0 meters)"
    assert note in plan.lines()


def test_the_dry_run_of_the_command_prints_the_squeezed_line(tmp_path):
    source = write_days(tmp_path / "one.nc", [0])
    args = ["convert", str(source), str(tmp_path / "s"), "--variable", "sst"]
    result = CliRunner().invoke(main, [*args, "--crs", "EPSG:4326", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "squeezed:   zlev (length 1, value 0 meters)" in result.output.splitlines()


def test_dimensions_with_no_coordinate_are_squeezed_without_a_value(tmp_path):
    values = day_values(0)[np.newaxis, np.newaxis, np.newaxis]
    _write(tmp_path / "x.nc", ("time", "height", "level", "lat", "lon"), values)
    plan = plan_conversion(tmp_path / "x.nc", variable="sst", crs="EPSG:4326", sample=False)
    notes = [n for n in plan.source.notes if n.startswith("squeezed:")]
    assert notes == ["squeezed:   height (length 1), level (length 1)"]


def test_a_dimension_that_is_not_length_one_says_how_to_choose(tmp_path):
    values = np.stack([day_values(0), day_values(1)])[np.newaxis]
    _write(tmp_path / "levels.nc", ("time", "level", "lat", "lon"), values, level=[0.0, 10.0])
    with pytest.raises(ValueError) as caught:
        plan_conversion(tmp_path / "levels.nc", variable="sst", crs="EPSG:4326", sample=False)
    message = str(caught.value)
    assert "'level'" in message and "length 2" in message
    assert "--dims band=level" in message
    assert "isel(level=0)" in message
    # Naming it as the band dimension keeps both levels, as bands.
    plan = plan_conversion(
        tmp_path / "levels.nc", variable="sst", crs="EPSG:4326", dims="band=level", sample=False
    )
    assert plan.source.info.band_names == ("0.0", "10.0")
    assert not any(n.startswith("squeezed:") for n in plan.source.notes)


def test_two_extra_dimensions_longer_than_one_name_both(tmp_path):
    values = np.zeros((1, 2, 3, HEIGHT, WIDTH), dtype=np.int16)
    _write(tmp_path / "x.nc", ("time", "a", "b", "lat", "lon"), values)
    with pytest.raises(ValueError, match=r"'a' \(length 2\), 'b' \(length 3\).*isel\(a=0, b=0\)"):
        plan_conversion(tmp_path / "x.nc", variable="sst", crs="EPSG:4326", sample=False)


def test_a_mask_variable_with_the_same_length_one_dimension_is_squeezed_too(tmp_path):
    ok = np.ones((1, 1, HEIGHT, WIDTH), dtype=np.uint8)
    ok[0, 0, 2, 3] = 0
    ds = xr.Dataset(
        {
            "sst": (("time", "zlev", "lat", "lon"), day_values(0)[np.newaxis, np.newaxis]),
            "ok": (("time", "zlev", "lat", "lon"), ok),
        },
        coords={
            "time": ("time", [0.0], {"units": "days since 2020-01-01"}),
            "zlev": ("zlev", [0.0]),
            "lat": ("lat", lat()),
            "lon": ("lon", lon()),
        },
    )
    ds.to_netcdf(tmp_path / "m.nc", engine="h5netcdf")
    out = tmp_path / "store"
    convert(tmp_path / "m.nc", out, variable="sst", mask_var="ok", crs="EPSG:4326", **OPTIONS)
    store = chronozarr.open_store(out)
    assert store.attrs.mask_variable == "mask"
    plane = store.read_mask(0)
    assert plane is not None
    assert np.array_equal(plane.astype(bool), ok[0, 0, ::-1].astype(bool))
