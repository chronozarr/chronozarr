"""Shared fixtures for TileRipper API tests.

Creates a minimal Zarr store with synthetic band data so endpoints can be
tested without real satellite imagery or network access.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import zarr


@pytest.fixture()
def zarr_store(tmp_path: Path):
    """Create a minimal Zarr store matching the B2_chunked format.

    Layout:
        {tmp_path}/stores/test_aoi/cs8/
            manifest.json
            r000_c000/
                stack.zarr   — shape (1, 4, 8, 8), uint16
    """
    aoi_name = "test_aoi"
    chunk_size = 512  # must match _get_aoi() lookup suffixes
    n_rows, n_cols = 1, 1
    months = ["2024-01"]
    epsg = 32631  # UTM 31N
    # 10m resolution affine: (pixel_x, 0, origin_x, 0, -pixel_y, origin_y)
    transform = [10.0, 0.0, 500000.0, 0.0, -10.0, 2600000.0]
    mosaic_h, mosaic_w = 8, 8  # small for speed, chunk_size is just metadata

    store_dir = tmp_path / "stores" / aoi_name / f"cs{chunk_size}"
    chunk_dir = store_dir / "r000_c000"
    chunk_dir.mkdir(parents=True)

    # Write manifest.json
    manifest = {
        "aoi": aoi_name,
        "label": "Test AOI",
        "chunk_size": chunk_size,
        "n_rows": n_rows,
        "n_cols": n_cols,
        "months": months,
        "epsg": epsg,
        "transform": transform,
        "mosaic_height": mosaic_h,
        "mosaic_width": mosaic_w,
        "source": "Sentinel-2 L2A",
        "composite_method": "monthly median, SCL cloud mask",
    }
    (store_dir / "manifest.json").write_text(json.dumps(manifest))

    # Write Zarr store: (n_months, 4, H, W) uint16
    rng = np.random.default_rng(42)
    data = rng.integers(100, 8000, size=(1, 4, mosaic_h, mosaic_w), dtype=np.uint16)
    z = zarr.open(str(chunk_dir / "stack.zarr"), mode="w", shape=data.shape, dtype="u2")
    z[:] = data

    return {
        "stores_root": tmp_path / "stores",
        "store_dir": store_dir,
        "aoi_name": aoi_name,
        "chunk_size": chunk_size,
        "months": months,
        "epsg": epsg,
        "transform": transform,
        "mosaic_height": mosaic_h,
        "mosaic_width": mosaic_w,
        "data": data,
    }


@pytest.fixture()
def api_client(zarr_store, monkeypatch):
    """FastAPI TestClient with a synthetic Zarr store backing AOI_CATALOG."""
    from fastapi.testclient import TestClient

    import spacetime.api.v1 as v1_mod
    from spacetime.api.jobs import JobDB
    from spacetime.api.v1 import AOI_CATALOG, _load_bands
    from spacetime.serve import _discover_aois, app

    # Clear any prior state
    AOI_CATALOG.clear()
    _load_bands.cache_clear()

    # Monkeypatch STORES_ROOT to point at our tmp dir
    monkeypatch.setattr("spacetime.serve.STORES_ROOT", zarr_store["stores_root"])

    # Initialize job DB in tmp dir
    job_db = JobDB(db_path=zarr_store["stores_root"].parent / "test_jobs.sqlite")
    monkeypatch.setattr(v1_mod, "JOB_DB", job_db)

    # Run discovery
    catalog = _discover_aois()
    AOI_CATALOG.update(catalog)

    client = TestClient(app, raise_server_exceptions=False)
    yield client

    # Cleanup
    AOI_CATALOG.clear()
    _load_bands.cache_clear()
