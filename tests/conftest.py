"""Shared fixtures for TileRipper API tests.

Creates a minimal v1 Zarr store with synthetic band data so endpoints can be
tested without real satellite imagery or network access.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import zarr


def _create_v1_store(
    stores_root: Path,
    aoi_name: str,
    months: list[str],
    epsg: int,
    transform: list[float],
    mosaic_h: int,
    mosaic_w: int,
    chunk_size: int,
    data: np.ndarray,
) -> Path:
    """Create a minimal v1 ChronoFabric store.

    Layout:
        stores_root/{aoi_name}/v1/
            manifest.json
            lod/0/chunks/r000_c000/stack.zarr
    """
    store_dir = stores_root / aoi_name / "v1"
    chunk_dir = store_dir / "lod" / "0" / "chunks" / "r000_c000"
    chunk_dir.mkdir(parents=True)

    z = zarr.open(str(chunk_dir / "stack.zarr"), mode="w", shape=data.shape, dtype="u2")
    z[:] = data

    manifest = {
        "version": "1.0.0",
        "epsg": epsg,
        "transform": transform,
        "mosaic_height": mosaic_h,
        "mosaic_width": mosaic_w,
        "bands": ["B02", "B03", "B04", "B08"],
        "dtype": "uint16",
        "nodata": 0,
        "months": months,
        "compressor": "zstd",
        "compressor_level": 5,
        "lod_levels": 1,
        "lod_factor": 2,
        "temporal": {
            "encoding": "star-delta",
            "anchor_interval": 6,
            "anchor_indices": [0],
            "delta_reference": {},
        },
        "lods": [
            {
                "level": 0,
                "resolution_m": 10.0,
                "grid_rows": 1,
                "grid_cols": 1,
                "chunk_size": chunk_size,
            }
        ],
        "cells": {"r000_c000": {"volatility": 0.01}},
    }
    (store_dir / "manifest.json").write_text(json.dumps(manifest))
    return store_dir


@pytest.fixture()
def zarr_store(tmp_path: Path):
    """Create a minimal v1 Zarr store."""
    aoi_name = "test_aoi"
    chunk_size = 512
    months = ["2024-01"]
    epsg = 32631
    transform = [10.0, 0.0, 500000.0, 0.0, -10.0, 2600000.0]
    mosaic_h, mosaic_w = 8, 8

    rng = np.random.default_rng(42)
    data = rng.integers(100, 8000, size=(1, 4, mosaic_h, mosaic_w), dtype=np.uint16)

    store_dir = _create_v1_store(
        tmp_path / "stores",
        aoi_name,
        months,
        epsg,
        transform,
        mosaic_h,
        mosaic_w,
        chunk_size,
        data,
    )

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

    AOI_CATALOG.clear()
    _load_bands.cache_clear()

    monkeypatch.setattr("spacetime.serve.STORES_ROOT", zarr_store["stores_root"])

    job_db = JobDB(db_path=zarr_store["stores_root"].parent / "test_jobs.sqlite")
    monkeypatch.setattr(v1_mod, "JOB_DB", job_db)

    catalog = _discover_aois()
    AOI_CATALOG.update(catalog)

    client = TestClient(app, raise_server_exceptions=False)
    yield client

    AOI_CATALOG.clear()
    _load_bands.cache_clear()
