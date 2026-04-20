"""TileRipper — FastAPI application entry point.

Mounts the v1 API router, auth middleware, and serves the product demo.

Usage:
    uv run --extra serve uvicorn spacetime.serve:app --reload --port 8765
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from spacetime.api.auth import AuthMiddleware
from spacetime.api.jobs import JobDB
from spacetime.api.naip import router as naip_router
from spacetime.api.v1 import AOI_CATALOG
from spacetime.api.v1 import router as v1_router

logger = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
STORES_ROOT = DATA_ROOT / "stores"


def _discover_aois() -> dict[str, dict]:
    """Scan stores directory for available AOIs via manifest.json files."""
    aois: dict[str, dict] = {}
    if not STORES_ROOT.exists():
        return aois

    for manifest_path in sorted(STORES_ROOT.rglob("manifest.json")):
        b2c_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text())

        # Check if this is a v1 store (ChronoFabric format)
        if manifest.get("version") == "1.0.0":
            # v1 store - key as aoi/v1
            aoi_name = b2c_dir.parent.name  # Parent dir is the AOI name
            key = f"{aoi_name}/v1"

            # Collect chunk IDs from lod/0/chunks/
            lod0_chunks_dir = b2c_dir / "lod" / "0" / "chunks"
            chunk_ids = (
                sorted(
                    d.name
                    for d in lod0_chunks_dir.iterdir()
                    if d.is_dir() and d.name.startswith("r")
                )
                if lod0_chunks_dir.is_dir()
                else []
            )

            # Build LODs info from manifest
            lods = manifest.get("lods", [])
            lod_levels = manifest.get("lod_levels", len(lods))

            aois[key] = {
                "aoi": aoi_name,
                "label": manifest.get("label", aoi_name),
                "version": "1.0.0",
                "store_dir": str(b2c_dir),
                "chunk_ids": chunk_ids,
                "months": manifest["months"],
                "n_months": len(manifest["months"]),
                "epsg": manifest.get("epsg"),
                "transform": manifest.get("transform"),
                "mosaic_height": manifest.get("mosaic_height"),
                "mosaic_width": manifest.get("mosaic_width"),
                "source": manifest.get("source", "Sentinel-2 L2A"),
                "composite_method": manifest.get(
                    "composite_method", "monthly median, SCL cloud mask"
                ),
                "lod_levels": lod_levels,
                "lods": lods,
                "temporal": manifest.get("temporal", {}),
                "cells": manifest.get("cells", {}),
                "compressor": manifest.get("compressor", "zstd"),
            }
            continue

        # Skip unknown manifest versions
        logger.warning("Skipping unknown store version %s at %s", manifest.get("version"), b2c_dir)
    return aois


def _configure_naip_aois() -> None:
    """Register NAIP AOIs. Discovery is lazy (on first request)."""
    from spacetime.api.naip import NAIP_CATALOG
    from spacetime.naip import NaipAoi

    naip_aois = [
        NaipAoi(
            name="iowa_ames_naip",
            label="Iowa Cropland (NAIP 1m)",
            bbox=(-93.66, 42.02, -93.63, 42.05),  # ~3km chip
            epsg=32615,
            resolution=1.0,
        ),
        NaipAoi(
            name="lake_mead_naip",
            label="Lake Mead (NAIP 1m)",
            bbox=(-114.77, 36.08, -114.74, 36.11),  # ~3km chip
            epsg=32611,
            resolution=1.0,
        ),
        NaipAoi(
            name="dc_mall_naip",
            label="Washington DC Mall (NAIP 0.6m)",
            bbox=(-77.05, 38.888, -77.03, 38.895),  # ~1.5km chip
            epsg=32618,
            resolution=0.6,
        ),
    ]
    NAIP_CATALOG.clear()
    for aoi in naip_aois:
        NAIP_CATALOG[aoi.name] = aoi
    logger.info("NAIP connector: %d AOIs configured (lazy discovery)", len(NAIP_CATALOG))


def _initialize_app_state() -> None:
    """Initialize shared application state on startup."""
    job_db = JobDB()
    import spacetime.api.v1 as v1_module

    v1_module.JOB_DB = job_db

    AOI_CATALOG.clear()
    catalog = _discover_aois()
    AOI_CATALOG.update(catalog)
    logger.info("TileRipper encoder: PIL")
    logger.info(
        "TileRipper ready — %d store(s): %s",
        len(AOI_CATALOG),
        list(AOI_CATALOG.keys()),
    )
    if not AOI_CATALOG:
        logger.warning("No stores found under %s", STORES_ROOT)
    logger.info("Job database initialized at %s", job_db.db_path)

    # NAIP connector (lazy — no network calls at startup)
    _configure_naip_aois()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _initialize_app_state()
    yield


app = FastAPI(
    title="TileRipper",
    version="0.1.0",
    description=(
        "Low-cost API for temporally coherent Sentinel-2 and Landsat basemaps, "
        "vegetation indices, and water layers — with maps plus values."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS — allow browser access from any origin for the demo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["X-Chunk-Width", "X-Chunk-Height", "X-Chunk-Encoding", "X-Timing-Ms"],
)

# Auth + rate limiting
app.add_middleware(AuthMiddleware)

# Mount v1 API
app.include_router(v1_router)

# Mount NAIP COG proxy

app.include_router(naip_router)

# Serve static assets (shaders.js, etc.)
_static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Serve the product demo page (ChronoFabric viewer)."""
    html_path = Path(__file__).parent / "static" / "viewer.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Demo not found</h1>", status_code=500)
    return HTMLResponse(html_path.read_text())
