"""Unit tests for v1 encoder: star-delta temporal encoding with multiscale pyramid.

Tests cover:
- Anchor schedule computation
- Encode/decode roundtrip
- Pyramid creation
- Manifest structure
- Delta reconstruction
- Volatility scoring
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import Affine

from spacetime.chunk import make_chunk_grid
from spacetime.encode.v1 import compute_anchor_schedule, decode_v1, encode_v1

# -----------------------------------------------------------------------------
# Anchor schedule tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_anchor_schedule_basic():
    """12 months, interval 6 → anchors at [0, 6], deltas reference nearest.

    Verify month 3 refs anchor 0, month 4 refs anchor 6.
    """
    anchor_indices, delta_reference = compute_anchor_schedule(12, 6)

    assert anchor_indices == [0, 6]
    assert delta_reference[3] == 0  # Month 3 is closer to anchor 0
    assert delta_reference[4] == 6  # Month 4 is closer to anchor 6
    assert delta_reference[5] == 6  # Month 5 is closer to anchor 6


@pytest.mark.unit
def test_anchor_schedule_all_anchors():
    """interval=1 → every month is an anchor, no deltas."""
    anchor_indices, delta_reference = compute_anchor_schedule(12, 1)

    assert anchor_indices == list(range(12))
    assert delta_reference == {}  # No deltas when every month is an anchor


@pytest.mark.unit
def test_anchor_schedule_single_month():
    """1 month → just one anchor at index 0."""
    anchor_indices, delta_reference = compute_anchor_schedule(1, 6)

    assert anchor_indices == [0]
    assert delta_reference == {}  # No deltas with only one month


# -----------------------------------------------------------------------------
# Encode/decode tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_encode_decode_roundtrip(tmp_path: Path):
    """Create synthetic monthly mosaics, encode, decode, verify exact match.

    - 3 months, 4 bands, 64x64 pixels
    - Use make_chunk_grid with chunk_size=64 (1 chunk)
    - Encode with encode_v1(n_lods=3, anchor_interval=2)
    - Decode each month at LOD 0 and verify exact match with input
    """
    rng = np.random.default_rng(42)
    n_bands = 4
    size = 64

    # Create synthetic monthly mosaics
    monthly_mosaics = {}
    months = ["2024-01", "2024-02", "2024-03"]
    for month in months:
        monthly_mosaics[month] = rng.integers(
            100, 8000, size=(n_bands, size, size), dtype=np.uint16
        )

    # Create chunk grid with chunk_size=64 (1 chunk)
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2500000.0)
    grid = make_chunk_grid(size, size, transform, 32631, chunk_size=64)

    # Encode
    store_dir = tmp_path / "store"
    encode_v1(monthly_mosaics, grid, store_dir, n_lods=3, anchor_interval=2)

    # Decode each month at LOD 0 and verify exact match
    for month_idx, month in enumerate(months):
        decoded = decode_v1(store_dir, lod=0, chunk_id="r000_c000", month_index=month_idx)
        expected = monthly_mosaics[month]
        assert decoded.shape == expected.shape
        assert np.array_equal(decoded, expected), f"Month {month} mismatch"


@pytest.mark.unit
def test_encode_creates_pyramid(tmp_path: Path):
    """Encode with n_lods=3 and verify pyramid structure.

    - Verify lod/0/, lod/1/, lod/2/ directories exist
    - Verify LOD 1 chunks are half the spatial size of LOD 0
    - Verify LOD 2 chunks are quarter the spatial size of LOD 0
    """
    rng = np.random.default_rng(42)
    n_bands = 4
    size = 64

    # Create synthetic monthly mosaics
    monthly_mosaics = {}
    months = ["2024-01", "2024-02"]
    for month in months:
        monthly_mosaics[month] = rng.integers(
            100, 8000, size=(n_bands, size, size), dtype=np.uint16
        )

    # Create chunk grid
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2500000.0)
    grid = make_chunk_grid(size, size, transform, 32631, chunk_size=64)

    # Encode with n_lods=3
    store_dir = tmp_path / "store"
    encode_v1(monthly_mosaics, grid, store_dir, n_lods=3, anchor_interval=2)

    # Verify LOD directories exist
    for lod in range(3):
        lod_dir = store_dir / "lod" / str(lod)
        assert lod_dir.is_dir(), f"LOD {lod} directory should exist"
        chunks_dir = lod_dir / "chunks"
        assert chunks_dir.is_dir(), f"LOD {lod} chunks directory should exist"

    # Verify spatial sizes
    import zarr

    lod0 = zarr.open(
        str(store_dir / "lod" / "0" / "chunks" / "r000_c000" / "stack.zarr"), mode="r"
    )
    lod1 = zarr.open(
        str(store_dir / "lod" / "1" / "chunks" / "r000_c000" / "stack.zarr"), mode="r"
    )
    lod2 = zarr.open(
        str(store_dir / "lod" / "2" / "chunks" / "r000_c000" / "stack.zarr"), mode="r"
    )

    # Shape is (n_months, n_bands, H, W)
    assert lod0.shape[2:] == (64, 64), "LOD 0 should be 64x64"
    assert lod1.shape[2:] == (32, 32), "LOD 1 should be 32x32 (half)"
    assert lod2.shape[2:] == (16, 16), "LOD 2 should be 16x16 (quarter)"


@pytest.mark.unit
def test_encode_manifest(tmp_path: Path):
    """Encode and read manifest.json, verify structure."""
    rng = np.random.default_rng(42)
    n_bands = 4
    size = 64

    # Create synthetic monthly mosaics
    monthly_mosaics = {}
    months = ["2024-01", "2024-02", "2024-03"]
    for month in months:
        monthly_mosaics[month] = rng.integers(
            100, 8000, size=(n_bands, size, size), dtype=np.uint16
        )

    # Create chunk grid
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2500000.0)
    grid = make_chunk_grid(size, size, transform, 32631, chunk_size=64)

    # Encode with anchor_interval=2
    store_dir = tmp_path / "store"
    encode_v1(monthly_mosaics, grid, store_dir, n_lods=2, anchor_interval=2)

    # Read manifest
    with open(store_dir / "manifest.json") as f:
        manifest = json.load(f)

    # Verify version
    assert manifest["version"] == "1.0.0"

    # Verify compressor
    assert manifest["compressor"] == "zstd"

    # Verify temporal encoding
    temporal = manifest["temporal"]
    assert temporal["encoding"] == "star-delta"
    assert temporal["anchor_indices"] == [0, 2]  # Months 0 and 2 are anchors

    # Verify lods array
    assert len(manifest["lods"]) == 2
    assert manifest["lods"][0]["level"] == 0
    assert manifest["lods"][1]["level"] == 1

    # Verify cells dict has volatility scores
    assert "cells" in manifest
    assert "r000_c000" in manifest["cells"]
    assert "volatility" in manifest["cells"]["r000_c000"]


@pytest.mark.unit
def test_decode_delta_reconstruction(tmp_path: Path):
    """2 months, interval=2 (month 0 anchor, month 1 delta).

    Make month 1 differ from month 0 by a known amount.
    Decode month 1 and verify the reconstruction matches.
    """
    n_bands = 4
    size = 64

    # Create month 0 as anchor
    month0 = np.full((n_bands, size, size), 5000, dtype=np.uint16)

    # Create month 1 with known offset from month 0
    offset = 100
    month1 = month0 + offset

    monthly_mosaics = {"2024-01": month0, "2024-02": month1}

    # Create chunk grid
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2500000.0)
    grid = make_chunk_grid(size, size, transform, 32631, chunk_size=64)

    # Encode with anchor_interval=2 (month 0 is anchor, month 1 is delta)
    store_dir = tmp_path / "store"
    encode_v1(monthly_mosaics, grid, store_dir, n_lods=1, anchor_interval=2)

    # Decode month 1 (delta month)
    decoded = decode_v1(store_dir, lod=0, chunk_id="r000_c000", month_index=1)

    # Verify reconstruction matches original month 1
    assert np.array_equal(decoded, month1), "Delta reconstruction should match original"


# -----------------------------------------------------------------------------
# Volatility tests
# -----------------------------------------------------------------------------


@pytest.mark.unit
def test_volatility_stable_scene(tmp_path: Path):
    """All months identical → volatility should be 0.0."""
    n_bands = 4
    size = 64

    # Create identical monthly mosaics
    identical_data = np.full((n_bands, size, size), 5000, dtype=np.uint16)
    monthly_mosaics = {
        "2024-01": identical_data.copy(),
        "2024-02": identical_data.copy(),
        "2024-03": identical_data.copy(),
    }

    # Create chunk grid
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2500000.0)
    grid = make_chunk_grid(size, size, transform, 32631, chunk_size=64)

    # Encode with anchor_interval=2
    store_dir = tmp_path / "store"
    result = encode_v1(monthly_mosaics, grid, store_dir, n_lods=1, anchor_interval=2)

    # Verify volatility is 0.0
    assert result["avg_volatility"] == 0.0

    # Verify manifest also shows 0.0
    with open(store_dir / "manifest.json") as f:
        manifest = json.load(f)
    assert manifest["cells"]["r000_c000"]["volatility"] == 0.0


@pytest.mark.unit
def test_volatility_changing_scene(tmp_path: Path):
    """Months with large differences → volatility > 0."""
    n_bands = 4
    size = 64

    # Create monthly mosaics with large differences
    monthly_mosaics = {
        "2024-01": np.full((n_bands, size, size), 1000, dtype=np.uint16),
        "2024-02": np.full((n_bands, size, size), 5000, dtype=np.uint16),
        "2024-03": np.full((n_bands, size, size), 9000, dtype=np.uint16),
    }

    # Create chunk grid
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2500000.0)
    grid = make_chunk_grid(size, size, transform, 32631, chunk_size=64)

    # Encode with anchor_interval=2
    store_dir = tmp_path / "store"
    result = encode_v1(monthly_mosaics, grid, store_dir, n_lods=1, anchor_interval=2)

    # Verify volatility is > 0
    assert result["avg_volatility"] > 0.0

    # Verify manifest also shows > 0
    with open(store_dir / "manifest.json") as f:
        manifest = json.load(f)
    assert manifest["cells"]["r000_c000"]["volatility"] > 0.0
