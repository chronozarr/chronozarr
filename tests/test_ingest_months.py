"""Scripts that read the monthly mosaics see every month whether it is .npz or .tif."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("planetary_computer")
pytest.importorskip("pystac_client")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "sentinel2_pc"))
import months  # noqa: E402

from tests.test_ingest_mosaic import BANDS, EPSG, _grid, mosaic  # noqa: E402

pytestmark = pytest.mark.unit


def mixed_dir(tmp_path: Path) -> tuple[Path, dict[str, dict]]:
    """2024-01 as a legacy .npz, 2024-02 as a GeoTIFF, same grid; returns the expected arrays."""
    grid = _grid()
    rng = np.random.default_rng(1)
    out = tmp_path / "mosaics"
    out.mkdir()
    expected = {}
    for key in ("2024-01", "2024-02"):
        bands = rng.integers(0, 9000, size=(4, grid.height, grid.width), dtype=np.uint16)
        count = rng.integers(0, 4, size=(grid.height, grid.width), dtype=np.uint16)
        expected[key] = {"bands": bands, "coverage": count.astype(np.float32) / 3}
    np.savez_compressed(
        out / "2024-01.npz",
        bands=expected["2024-01"]["bands"],
        coverage=expected["2024-01"]["coverage"],
        transform=np.array(list(grid.transform)[:6]),
        epsg=np.array(EPSG),
        band_names=np.array(list(BANDS)),
    )
    count = np.rint(expected["2024-02"]["coverage"] * 3).astype(np.uint16)
    mosaic.save_month(
        out / "2024-02.tif",
        grid,
        EPSG,
        list(BANDS),
        expected["2024-02"]["bands"],
        count,
        {
            "band_names": list(BANDS),
            "scenes_searched": 3,
            "scenes_failed": [],
            "scenes_failed_windows": {},
            "carried_from": [],
        },
    )
    return out, expected


def test_months_reads_both_formats_with_the_same_keys(tmp_path):
    out, expected = mixed_dir(tmp_path)
    paths = months.month_files(out)
    assert [p.name for p in paths] == ["2024-01.npz", "2024-02.tif"]
    first, second = (months.month_arrays(p) for p in paths)
    for key, month in zip(expected, (first, second), strict=True):
        np.testing.assert_array_equal(month["bands"], expected[key]["bands"])
        np.testing.assert_array_equal(month["coverage"], expected[key]["coverage"])
    assert first["band_names"] == second["band_names"] == list(BANDS)
    assert first["epsg"] == second["epsg"] == EPSG
    np.testing.assert_array_equal(first["transform"], second["transform"])
    assert months.month_path(out, "2024-02") == out / "2024-02.tif"
    (out / "2024-02.npz").write_bytes((out / "2024-01.npz").read_bytes())
    with pytest.raises(ValueError, match=r"both a \.tif and an \.npz"):
        months.month_files(out)


def test_the_mosaic_readers_see_every_month(tmp_path):
    """The water-mask builder, the re-encoder and the PNG frame renderer read a directory
    holding both formats whole (they used to read only *.npz and skip GeoTIFF months)."""
    out, expected = mixed_dir(tmp_path)
    for directory in ("examples/water_masks", "examples/png_frames", "scripts"):
        sys.path.insert(0, str(ROOT / directory))
    water = importlib.import_module("build_water_stack")
    reencode = importlib.import_module("reencode_aoi")
    frames = importlib.import_module("render_frames")

    _, found = water.scan(months.month_files(out))
    assert [m.path.stem for m in found] == ["2024-01", "2024-02"]
    assert [m.valid_px for m in found] == [
        int(np.count_nonzero(expected[k]["coverage"] > 0)) for k in expected
    ]
    paths = reencode.mosaic_paths(out)
    grid = reencode.read_grid(paths[0])
    stacked = list(reencode.iter_mosaics(paths, grid))
    for key, bands in zip(expected, stacked, strict=True):
        np.testing.assert_array_equal(bands, expected[key]["bands"])
    window = frames.read_window(paths[1], 0, 0, 8)
    np.testing.assert_array_equal(window.valid, expected["2024-02"]["coverage"][:8, :8] > 0)
