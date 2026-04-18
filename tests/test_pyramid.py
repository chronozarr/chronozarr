"""Unit tests for multiscale pyramid construction."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest
import zarr
from rasterio.transform import Affine

from spacetime.chunk import make_chunk_grid
from spacetime.pyramid import (
    build_pyramid,
    compute_pyramid_levels,
    downsample_2x,
    scale_transform,
    write_zarr_level,
)

logger = logging.getLogger(__name__)


@pytest.mark.unit
def test_downsample_even_dimensions():
    data = np.arange(4 * 8 * 8, dtype=np.uint16).reshape(4, 8, 8)

    result = downsample_2x(data)

    expected_band0 = np.array(
        [
            [5, 7, 9, 11],
            [21, 23, 25, 27],
            [37, 39, 41, 43],
            [53, 55, 57, 59],
        ],
        dtype=np.uint16,
    )
    expected = np.stack(
        [expected_band0 + np.uint16(64 * band) for band in range(4)],
        axis=0,
    )
    assert result.shape == (4, 4, 4)
    assert np.array_equal(result, expected)


@pytest.mark.unit
def test_downsample_odd_dimensions():
    data = np.arange(4 * 7 * 5, dtype=np.uint16).reshape(4, 7, 5)

    result = downsample_2x(data)

    assert result.shape == (4, 4, 3)


@pytest.mark.unit
def test_downsample_preserves_dtype():
    data = np.arange(4 * 7 * 5, dtype=np.uint16).reshape(4, 7, 5)

    result = downsample_2x(data)

    assert result.dtype == np.uint16


@pytest.mark.unit
def test_downsample_spectral_fidelity():
    data = np.zeros((4, 2, 2), dtype=np.uint16)
    data[0] = np.array([[100, 200], [300, 400]], dtype=np.uint16)

    result = downsample_2x(data)

    assert result[0, 0, 0] == 250


@pytest.mark.unit
def test_downsample_nodata_propagation():
    data = np.zeros((4, 8, 8), dtype=np.uint16)

    result = downsample_2x(data)

    assert np.array_equal(result, np.zeros((4, 4, 4), dtype=np.uint16))


@pytest.mark.unit
def test_compute_levels_sahara():
    assert compute_pyramid_levels(2814, 2616, 512) == 3


@pytest.mark.unit
def test_compute_levels_single_chunk():
    assert compute_pyramid_levels(512, 512, 512) == 0


@pytest.mark.unit
def test_compute_levels_just_over():
    assert compute_pyramid_levels(513, 513, 512) == 1


@pytest.mark.unit
def test_scale_transform():
    transform = Affine(10, 0, 500000, 0, -10, 2600000)

    result = scale_transform(transform, 2)

    assert result == Affine(20, 0, 500000, 0, -20, 2600000)


@pytest.mark.unit
def test_build_pyramid_creates_files(tmp_path: Path):
    rng = np.random.default_rng(42)
    monthly_mosaics = {
        "2024-01": rng.integers(100, 8000, (4, 16, 16), dtype=np.uint16),
    }
    grid = make_chunk_grid(16, 16, Affine(10, 0, 0, 0, -10, 0), 32631, chunk_size=8)
    store_dir = tmp_path / "store"

    write_zarr_level(monthly_mosaics, grid, store_dir)
    metadata = build_pyramid(monthly_mosaics, grid, store_dir, chunk_size=8)

    assert metadata
    assert (store_dir / "pyramid" / "1").is_dir()
    assert sorted(
        path.name for path in (store_dir / "pyramid" / "1").iterdir() if path.is_dir()
    ) == ["r000_c000"]

    level0 = zarr.open(str(store_dir / "r000_c000" / "stack.zarr"), mode="r")
    level1 = zarr.open(str(store_dir / "pyramid" / "1" / "r000_c000" / "stack.zarr"), mode="r")
    assert level0.shape == (1, 4, 8, 8)
    assert level1.shape == (1, 4, 8, 8)


@pytest.mark.unit
def test_build_pyramid_metadata(tmp_path: Path):
    rng = np.random.default_rng(42)
    monthly_mosaics = {
        "2024-01": rng.integers(100, 8000, (4, 16, 16), dtype=np.uint16),
    }
    grid = make_chunk_grid(16, 16, Affine(10, 0, 0, 0, -10, 0), 32631, chunk_size=8)
    store_dir = tmp_path / "store"

    write_zarr_level(monthly_mosaics, grid, store_dir)
    metadata = build_pyramid(monthly_mosaics, grid, store_dir, chunk_size=8)

    assert metadata == [
        {
            "level": 1,
            "mosaic_height": 8,
            "mosaic_width": 8,
            "n_rows": 1,
            "n_cols": 1,
            "resolution_m": 20.0,
            "transform": [20.0, 0.0, 0.0, 0.0, -20.0, 0.0],
        }
    ]


@pytest.mark.unit
def test_write_zarr_level_flat_layout(tmp_path: Path):
    rng = np.random.default_rng(42)
    monthly_mosaics = {
        "2024-01": rng.integers(100, 8000, (4, 16, 16), dtype=np.uint16),
    }
    grid = make_chunk_grid(16, 16, Affine(10, 0, 0, 0, -10, 0), 32631, chunk_size=8)
    root_dir = tmp_path / "store"

    write_zarr_level(monthly_mosaics, grid, root_dir)

    assert (root_dir / "r000_c000" / "stack.zarr").exists()
    assert not (root_dir / "b2_chunked").exists()
