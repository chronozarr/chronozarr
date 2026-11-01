"""The water example checks recursive masked overviews, not unsupported area conservation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

pytest.importorskip("rasterio")

import chronozarr
from chronozarr.schema import Band

pytestmark = pytest.mark.unit


def load_checker():
    path = Path(__file__).resolve().parents[1] / "examples/water_masks/check_water_stack.py"
    spec = importlib.util.spec_from_file_location("water_checker", path)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker


def test_recursive_fraction_with_unequal_valid_support():
    checker = load_checker()
    water = np.zeros((4, 4), dtype=np.int16)
    valid = np.ones((4, 4), dtype=bool)
    water[0, 0] = 10000
    valid[:2, :2] = False
    valid[0, 0] = True
    one, mask_one = checker.next_overview(water, valid)
    np.testing.assert_array_equal(one, [[10000, 0], [0, 0]])
    two, mask_two = checker.next_overview(one, mask_one)
    assert two.item() == 2500 and mask_two.item()
    assert water[valid].mean() / 10000 == 1 / 13


def test_overview_reference_replicates_odd_edges_and_zeros_empty_blocks():
    checker = load_checker()
    values = np.array([[0, 0, 10000], [0, 0, 10000], [0, 10000, 10000]])
    valid = np.ones((3, 3), dtype=bool)
    valid[:2, :2] = False
    one, mask = checker.next_overview(values, valid)
    np.testing.assert_array_equal(one, [[0, 10000], [5000, 10000]])
    np.testing.assert_array_equal(mask, [[False, True], [True, True]])


def test_checker_accepts_real_pyramid_with_nonconserved_water_share(tmp_path, monkeypatch, capsys):
    checker = load_checker()
    valid = np.ones((4, 4), dtype=np.uint8)
    valid[:2, :2] = 0
    valid[0, 0] = 1
    bands = np.full((4, 4, 4), 1000, dtype=np.uint16)
    bands[3] = 3000  # land: green < nir
    bands[1, 0, 0], bands[3, 0, 0] = 3000, 1000  # water: green > nir
    mosaics = tmp_path / "mosaics/synthetic"
    mosaics.mkdir(parents=True)
    transform = [10, 0, 485650, 0, -10, 9169880]
    month = mosaics / "2024-01.npz"
    np.savez(
        month,
        bands=bands,
        coverage=valid,
        transform=transform,
        epsg=32718,
        band_names=["B02", "B03", "B04", "B08"],
    )
    ndwi, water, mask = checker.recompute(month, None, 0)
    store = tmp_path / "stores/synthetic/water-1"
    da = xr.DataArray(
        np.stack([ndwi, water])[None],
        dims=("time", "band", "y", "x"),
        coords={"time": [np.datetime64("2024-01-01")], "band": ["ndwi", "water"]},
        attrs={"crs": "EPSG:32718", "transform": transform},
    )
    chronozarr.encode(
        da,
        store,
        chunk_size=2,
        n_lods=3,
        mask=mask[None],
        coverage=valid[None],
        nodata=0,
        bands=[
            Band("ndwi", scale=1e-4, units="index"),
            Band("water", scale=1e-4, units="fraction"),
        ],
    )
    (store.parent / "water-1.months.csv").write_text(
        "month,status,threshold,valid_frac,water_frac\n2024-01,written,0,0.8125,0.076923\n"
    )
    monkeypatch.setattr(checker, "DATA", tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["check_water_stack.py", "--aoi", "synthetic", "--month", "2024-01"]
    )
    checker.main()
    output = capsys.readouterr().out
    assert "lod 2: 0.25000" in output and "all checks passed" in output
