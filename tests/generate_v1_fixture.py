"""Generate canonical v1 fixture for regression testing.

Creates a tiny v1 store (3 months, 4 bands, 32x32, 2 LODs) and saves
the expected decoded values. Run this script to regenerate the fixture
if the format intentionally changes.

Usage:
    uv run python tests/generate_v1_fixture.py
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from rasterio.transform import Affine

from spacetime.chunk import make_chunk_grid
from spacetime.encode.v1 import decode_v1, encode_v1

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "v1_canonical"
SEED = 12345
N_BANDS = 4
SIZE = 32
MONTHS = ["2024-01", "2024-02", "2024-03"]
CHUNK_SIZE = 32
N_LODS = 2
ANCHOR_INTERVAL = 2


def generate() -> None:
    rng = np.random.default_rng(SEED)

    # Create synthetic monthly mosaics with realistic reflectance values
    monthly_mosaics = {}
    base = rng.integers(500, 4000, size=(N_BANDS, SIZE, SIZE), dtype=np.uint16)
    for i, month in enumerate(MONTHS):
        # Each month adds small perturbation to base
        perturbation = rng.integers(-200, 200, size=(N_BANDS, SIZE, SIZE))
        monthly_mosaics[month] = (
            (base.astype(np.int32) + perturbation * i).clip(0, 65535).astype(np.uint16)
        )

    # Build grid
    transform = Affine(10.0, 0.0, 500000.0, 0.0, -10.0, 2500000.0)
    grid = make_chunk_grid(SIZE, SIZE, transform, 32631, chunk_size=CHUNK_SIZE)

    # Clean and encode
    import shutil

    if FIXTURE_DIR.exists():
        shutil.rmtree(FIXTURE_DIR)

    store_dir = FIXTURE_DIR / "store"
    encode_v1(monthly_mosaics, grid, store_dir, n_lods=N_LODS, anchor_interval=ANCHOR_INTERVAL)

    # Save expected decoded values for each month at LOD 0
    expected = {}
    for month_idx in range(len(MONTHS)):
        decoded = decode_v1(store_dir, lod=0, chunk_id="r000_c000", month_index=month_idx)
        expected[f"month_{month_idx}"] = decoded

    np.savez_compressed(FIXTURE_DIR / "expected.npz", **expected)

    # Save input values for reference
    input_data = {f"input_{month}": data for month, data in monthly_mosaics.items()}
    np.savez_compressed(FIXTURE_DIR / "input.npz", **input_data)

    # Verify roundtrip before saving
    for month_idx, month in enumerate(MONTHS):
        decoded = expected[f"month_{month_idx}"]
        original = monthly_mosaics[month]
        assert np.array_equal(decoded, original), f"Roundtrip failed for {month}"

    print(f"Fixture generated at {FIXTURE_DIR}")
    print(f"  Store: {store_dir}")
    print(f"  Expected: {FIXTURE_DIR / 'expected.npz'}")
    print(f"  Input: {FIXTURE_DIR / 'input.npz'}")

    # Print manifest for reference
    with open(store_dir / "manifest.json") as f:
        manifest = json.load(f)
    print(f"  Months: {manifest['months']}")
    print(f"  Anchors: {manifest['temporal']['anchor_indices']}")
    print(f"  LODs: {len(manifest['lods'])}")


if __name__ == "__main__":
    generate()
