"""TileRipper — FastAPI application entry point.

Mounts the v1 API router, auth middleware, and serves the product demo.

Usage:
    uv run --extra serve uvicorn spacetime.serve:app --reload --port 8765
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from spacetime.api.auth import AuthMiddleware
from spacetime.api.jobs import JobDB
from spacetime.api.v1 import AOI_CATALOG, HAS_PYVIPS
from spacetime.api.v1 import JOB_DB as v1_job_db
from spacetime.api.v1 import router as v1_router

logger = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
STORES_ROOT = DATA_ROOT / "stores"

app = FastAPI(
    title="TileRipper",
    version="0.1.0",
    description=(
        "Low-cost API for temporally coherent Sentinel-2 and Landsat basemaps, "
        "vegetation indices, and water layers — with maps plus values."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — allow browser access from any origin for the demo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Auth + rate limiting
app.add_middleware(AuthMiddleware)

# Mount v1 API
app.include_router(v1_router)


def _discover_aois() -> dict[str, dict]:
    """Scan stores directory for available AOIs via manifest.json files."""
    aois: dict[str, dict] = {}
    if not STORES_ROOT.exists():
        return aois

    for manifest_path in sorted(STORES_ROOT.rglob("manifest.json")):
        b2c_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text())

        aoi_name = manifest["aoi"]
        chunk_size = manifest["chunk_size"]
        n_rows = manifest["n_rows"]
        n_cols = manifest["n_cols"]

        chunk_ids = sorted(
            f"r{r:03d}_c{c:03d}"
            for r in range(n_rows)
            for c in range(n_cols)
            if (b2c_dir / f"r{r:03d}_c{c:03d}").is_dir()
        )

        key = f"{aoi_name}/cs{chunk_size}"
        aois[key] = {
            "aoi": aoi_name,
            "label": manifest.get("label", aoi_name),
            "chunk_size": chunk_size,
            "store_dir": str(b2c_dir),
            "chunk_ids": chunk_ids,
            "months": manifest["months"],
            "n_rows": n_rows,
            "n_cols": n_cols,
            "n_months": len(manifest["months"]),
            "epsg": manifest.get("epsg"),
            "transform": manifest.get("transform"),
            "mosaic_height": manifest.get("mosaic_height"),
            "mosaic_width": manifest.get("mosaic_width"),
            "source": manifest.get("source", "Sentinel-2 L2A"),
            "composite_method": manifest.get("composite_method", "monthly median, SCL cloud mask"),
        }
    return aois


@app.on_event("startup")
def startup():
    # Initialize job database
    job_db = JobDB()
    v1_job_db.__class__.JOBS = job_db  # Access the module-level JOB_DB via the class
    import spacetime.api.v1 as v1_module

    v1_module.JOB_DB = job_db

    # Discover existing AOIs
    catalog = _discover_aois()
    AOI_CATALOG.update(catalog)
    encoder = "pyvips" if HAS_PYVIPS else "PIL"
    logger.info("TileRipper encoder: %s", encoder)
    logger.info(
        "TileRipper ready — %d store(s): %s",
        len(AOI_CATALOG),
        list(AOI_CATALOG.keys()),
    )
    if not AOI_CATALOG:
        logger.warning("No stores found under %s", STORES_ROOT)
    logger.info("Job database initialized at %s", job_db.db_path)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    """Serve the product demo page."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Demo not found</h1>", status_code=500)
    return HTMLResponse(html_path.read_text())
