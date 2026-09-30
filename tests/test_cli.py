"""Command line: encode from Zarr and GeoTIFF inputs, validate, info."""

from __future__ import annotations

import numpy as np
import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr.cli import main
from tests.synthetic import make_da, make_truth

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
    result = _run("encode", str(source), str(out), "--chunk-size", "16", "--anchor-interval", "2")
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output
    assert np.array_equal(chronozarr.open_store(out).to_xarray().values, truth)

    ok = _run("validate", str(out))
    assert ok.exit_code == 0
    assert "conforms to chronozarr 0.1.0" in ok.output

    info = _run("info", str(out))
    assert info.exit_code == 0
    for expected in (
        "chronozarr 0.1.0",
        "EPSG:32631",
        "times:     4",
        "2 anchors, 2 deltas",
        "grid 3x4",
    ):
        assert expected in info.output


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
