"""`convert` keeps the native CRS of its sources when it has no EPSG code."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import xarray as xr

import chronozarr
from chronozarr import schema
from chronozarr.convert import convert, plan_conversion
from tests.crs_samples import LCC_WKT2, SINUSOIDAL_WKT2
from tests.synthetic import BANDS, CRS, TRANSFORM, make_times, make_truth

pytestmark = pytest.mark.unit

HEIGHT, WIDTH, N_TIME = 40, 50, 4
CHUNK = 16
CASES = [pytest.param(LCC_WKT2, id="lcc"), pytest.param(SINUSOIDAL_WKT2, id="sinusoidal")]


@pytest.fixture(scope="module")
def truth() -> np.ndarray:
    return make_truth(N_TIME, 2, HEIGHT, WIDTH)


def assert_values_equal(store_path: Path, truth: np.ndarray) -> None:
    opened = chronozarr.open_store(store_path)
    for t in range(len(truth)):
        np.testing.assert_array_equal(opened.read(t), truth[t])


# --- convert --------------------------------------------------------------------------------


def write_tif(path: Path, array: np.ndarray, crs: str) -> Path:
    import rasterio
    from rasterio.transform import Affine

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[1],
        width=array.shape[2],
        count=array.shape[0],
        dtype=array.dtype,
        crs=crs,
        transform=Affine(*TRANSFORM),
        nodata=0,
    ) as dst:
        dst.write(array)
    return path


def manifest_of(folder: Path, truth: np.ndarray, crs: str) -> Path:
    folder.mkdir()
    paths = [write_tif(folder / f"scene_{t}.tif", truth[t], crs) for t in range(len(truth))]
    dates = [f"2024-0{t + 1}-01" for t in range(len(truth))]
    manifest = folder / "manifest.csv"
    manifest.write_text(
        "\n".join(["uri,datetime", *(f"{p},{d}" for p, d in zip(paths, dates, strict=True))])
        + "\n"
    )
    return manifest


@pytest.mark.parametrize("wkt2", CASES)
def test_convert_keeps_the_native_crs_of_geotiffs_and_every_value(tmp_path, truth, wkt2):
    rasterio_crs = pytest.importorskip("rasterio.crs").CRS
    manifest = manifest_of(tmp_path / "cogs", truth, wkt2)
    out = tmp_path / "store"

    report = convert(manifest, out, chunk_size=CHUNK)

    assert report.plan.source.info.grid.describe().startswith('WKT2 "')
    assert schema.validate_report(out).problems == []
    opened = chronozarr.open_store(out)
    assert opened.attrs.spec_version == "0.4.0"
    assert opened.crs.code is None
    assert rasterio_crs.from_user_input(opened.crs.to_string()) == rasterio_crs.from_wkt(wkt2)
    assert opened.levels[0].transform == TRANSFORM
    assert_values_equal(out, truth)


def test_convert_with_the_source_crs_given_does_not_warp(tmp_path, truth):
    manifest = manifest_of(tmp_path / "cogs", truth, LCC_WKT2)
    plan = plan_conversion(manifest, crs=LCC_WKT2, sample=False)
    assert plan.source.warped == frozenset()
    assert plan.source.info.grid.transform == TRANSFORM


def test_convert_with_another_crs_warps_as_it_does_for_an_epsg_pair(tmp_path, truth):
    manifest = manifest_of(tmp_path / "cogs", truth, LCC_WKT2)
    plan = plan_conversion(manifest, crs=SINUSOIDAL_WKT2, resampling="nearest", sample=False)
    assert plan.source.warped == frozenset(range(len(truth)))


def test_a_geotiff_of_an_epsg_crs_still_gives_the_epsg_code(tmp_path, truth):
    manifest = manifest_of(tmp_path / "cogs", truth, CRS)
    out = tmp_path / "store"
    convert(manifest, out, chunk_size=CHUNK)
    opened = chronozarr.open_store(out)
    assert (opened.attrs.spec_version, opened.crs.code) == ("0.3.0", CRS)


def test_convert_keeps_the_crs_of_a_zarr_source_that_has_no_epsg_code(tmp_path, truth):
    a, _, c, _, e, f = TRANSFORM
    da = xr.DataArray(
        truth,
        dims=("time", "band", "y", "x"),
        coords={
            "time": make_times(len(truth)),
            "band": list(BANDS),
            "y": f + e * (np.arange(HEIGHT) + 0.5),
            "x": c + a * (np.arange(WIDTH) + 0.5),
        },
        name="reflectance",
        attrs={"crs": SINUSOIDAL_WKT2},
    )
    source = tmp_path / "in.zarr"
    da.to_dataset().to_zarr(source, zarr_format=2, consolidated=True)
    out = tmp_path / "store"
    convert(source, out, chunk_size=CHUNK)
    opened = chronozarr.open_store(out)
    assert opened.attrs.spec_version == "0.4.0"
    assert opened.crs.wkt2 is not None
    assert_values_equal(out, truth)
