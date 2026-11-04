"""CF packing on the axes and data of NetCDF and Zarr sources."""

import numpy as np
import pytest
import xarray as xr

from chronozarr import open_store, validate
from chronozarr.convert import convert

pytestmark = pytest.mark.unit

pytest.importorskip("rasterio")


@pytest.mark.parametrize("source_format", ["netcdf", "zarr2", "zarr3"])
@pytest.mark.parametrize("use_mask", [False, True])
def test_convert_packed_axes_and_unsigned_values(tmp_path, source_format, use_mask):
    values = np.array([[[-3, -5], [-7, -1]], [[-8, -4], [-6, -9]]], dtype="int16")
    mask = np.ones_like(values, dtype="uint8")
    mask[1, 0, 0] = 0
    source = xr.Dataset(
        {
            "v": (
                ("time", "y", "x"),
                values,
                {
                    "_Unsigned": "true",
                    "_FillValue": np.int16(-1),
                    "scale_factor": 0.5,
                    "add_offset": 1.0,
                },
            ),
            "valid": (("time", "y", "x"), mask),
        },
        coords={
            "time": (
                "time",
                [0, 1],
                {"units": "days since 2024-01-01", "scale_factor": 2.0, "add_offset": 3.0},
            ),
            "x": ("x", [1, 2], {"scale_factor": 10.0, "add_offset": 500000.0}),
            "y": ("y", [2, 1], {"scale_factor": 10.0, "add_offset": 4200000.0}),
        },
        attrs={"crs": "EPSG:32618"},
    )
    if source_format == "netcdf":
        pytest.importorskip("h5netcdf")
        path = tmp_path / "packed.nc"
        source.to_netcdf(path, engine="h5netcdf")
        opened_source = xr.open_dataset(path, engine="h5netcdf")
    else:
        path = tmp_path / "packed.zarr"
        consolidated = source_format == "zarr2"
        source.to_zarr(path, zarr_format=int(source_format[-1]), consolidated=consolidated)
        opened_source = xr.open_zarr(path, consolidated=consolidated, chunks=None)

    with opened_source as direct:
        expected = direct["v"].where(direct["valid"] != 0) if use_mask else direct["v"]
        out = tmp_path / "output.zarr"
        convert(path, out, variable="v", mask_var="valid" if use_mask else None, n_lods=1)
        store = open_store(out)
        assert validate(out) == []
        decoded = store.to_xarray(physical=True).isel(band=0)
        for axis in ("time", "y", "x"):
            np.testing.assert_array_equal(decoded[axis].values, direct[axis].values)
        np.testing.assert_array_equal(decoded.values, expected.values)
        assert store.dtype == np.dtype("uint16")
        assert store.nodata == (None if use_mask else 65535)
        np.testing.assert_array_equal(store.read(0)[0], values[0].view("uint16"))
        assert decoded.values[0, 0, 0] == 32767.5
        assert np.isnan(decoded.values[0, 1, 1])


def test_netcdf_series_compares_the_decoded_grid(tmp_path):
    pytest.importorskip("h5netcdf")
    paths = tmp_path / "series"
    paths.mkdir()
    for packed in (False, True):
        source = xr.Dataset(
            {"v": (("time", "y", "x"), np.full((1, 2, 2), 7 + packed, dtype="int16"))},
            coords={
                "time": (
                    "time",
                    [0],
                    {"units": "days since 2024-01-01"} | ({"add_offset": 3.0} if packed else {}),
                ),
                "x": (
                    "x",
                    [1, 2] if packed else [500010, 500020],
                    {"scale_factor": 10.0, "add_offset": 500000.0} if packed else {},
                ),
                "y": (
                    "y",
                    [2, 1] if packed else [4200020, 4200010],
                    {"scale_factor": 10.0, "add_offset": 4200000.0} if packed else {},
                ),
            },
            attrs={"crs": "EPSG:32618"},
        )
        source.to_netcdf(paths / f"{int(packed)}.nc", engine="h5netcdf")
    out = tmp_path / "output.zarr"
    convert(paths, out, n_lods=1)
    store = open_store(out)
    np.testing.assert_array_equal(
        store.times, np.array(["2024-01-01", "2024-01-04"], "datetime64[D]")
    )
    np.testing.assert_array_equal(store.to_xarray().x.values, [500010, 500020])
    np.testing.assert_array_equal(store.to_xarray().values[:, 0, 0, 0], [7, 8])
