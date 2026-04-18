"""TileRipper cluster worker — polls for jobs and runs the ingestion pipeline.

Usage:
    uv run python scripts/worker.py --api-url https://tileripper.com --once
    uv run python scripts/worker.py  # continuous polling
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import traceback
from pathlib import Path

import click
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("worker")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def claim_job(client: httpx.Client) -> dict | None:
    """Poll /v1/jobs/next for the next pending job."""
    r = client.get("/v1/jobs/next")
    if r.status_code == 204:
        return None
    if r.status_code == 503:
        logger.error("Worker key not configured on server")
        sys.exit(1)
    r.raise_for_status()
    return r.json()


def report_complete(client: httpx.Client, job_id: str, store_path: str) -> None:
    """Mark job as complete."""
    r = client.post(f"/v1/jobs/{job_id}/complete", json={"store_path": store_path})
    r.raise_for_status()
    logger.info("Job %s marked complete", job_id)


def report_failure(client: httpx.Client, job_id: str, error: str) -> None:
    """Mark job as failed."""
    r = client.post(f"/v1/jobs/{job_id}/fail", json={"error": error[:2000]})
    r.raise_for_status()
    logger.info("Job %s marked failed", job_id)


def run_pipeline(job: dict) -> str:
    """Run the ingestion pipeline for a job. Returns the store path."""
    from spacetime.catalog import search_scenes_by_month
    from spacetime.chunk import make_chunk_grid
    from spacetime.mosaic import build_monthly_mosaics

    aoi_name = job["aoi_name"]
    bbox = (job["bbox_west"], job["bbox_south"], job["bbox_east"], job["bbox_north"])
    epsg = job["epsg"]
    chunk_size = job.get("chunk_size", 512)
    start_month = job["start_month"]
    end_month = job["end_month"]

    # Convert YYYY-MM to date strings for STAC search
    start_date = f"{start_month}-01"
    # End date: last day of end_month
    end_year, end_mon = int(end_month[:4]), int(end_month[5:7])
    if end_mon == 12:
        end_date = f"{end_year}-12-31"
    else:
        from datetime import date

        next_month = date(end_year, end_mon + 1, 1)
        last_day = next_month.replace(day=1) - __import__("datetime").timedelta(days=1)
        end_date = last_day.isoformat()

    # Phase 1: Catalog + Mosaic
    logger.info("Searching scenes for %s [%s → %s]", aoi_name, start_date, end_date)
    by_month = search_scenes_by_month(bbox, start_date, end_date, max_cloud_pct=80.0)

    mosaic_dir = DATA / "mosaics" / aoi_name
    logger.info("Building %d monthly mosaics", len(by_month))
    mosaic_paths = build_monthly_mosaics(by_month, bbox, target_epsg=epsg, output_dir=mosaic_dir)

    if not mosaic_paths:
        raise RuntimeError(f"No mosaics produced for {aoi_name}")

    # Load mosaics
    from spacetime.mosaic import load_mosaic

    mosaics = {}
    for path in sorted(mosaic_paths.values()):
        month_key = path.stem
        m = load_mosaic(path)
        mosaics[month_key] = m["bands"]

    # Get grid from first mosaic
    first_path = next(iter(sorted(mosaic_paths.values())))
    first_meta = load_mosaic(first_path)
    bands = first_meta["bands"]
    transform = first_meta["transform"]

    grid = make_chunk_grid(bands.shape[1], bands.shape[2], transform, epsg, chunk_size)

    # Phase 2: Encode level 0 (flat layout)
    store_dir = DATA / "stores" / aoi_name / f"cs{chunk_size}"
    logger.info("Encoding level 0 to %s", store_dir)
    from spacetime.pyramid import build_pyramid, write_zarr_level

    write_zarr_level(mosaics, grid, store_dir)

    # Phase 3: Build pyramid levels
    logger.info("Building pyramid levels")
    pyramid_levels = build_pyramid(mosaics, grid, store_dir, chunk_size)
    logger.info("Built %d pyramid levels", len(pyramid_levels))

    # Write manifest.json
    months = sorted(mosaics.keys())
    manifest = {
        "aoi": aoi_name,
        "label": aoi_name.replace("_", " ").title(),
        "chunk_size": chunk_size,
        "n_rows": grid.n_rows,
        "n_cols": grid.n_cols,
        "months": months,
        "epsg": epsg,
        "transform": list(transform) if not isinstance(transform, list) else transform,
        "mosaic_height": bands.shape[1],
        "mosaic_width": bands.shape[2],
        "source": "Sentinel-2 L2A",
        "composite_method": "monthly median, SCL cloud mask",
    }
    if pyramid_levels:
        manifest["pyramid"] = {
            "n_levels": len(pyramid_levels),
            "levels": pyramid_levels,
        }
    manifest_path = store_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info("Wrote manifest to %s", manifest_path)

    return str(store_dir)


def rsync_to_vps(store_path: str, aoi_name: str, vps_host: str) -> None:
    """Rsync the store to the VPS."""
    remote = f"{vps_host}:~/tile_ripper/data/stores/{aoi_name}/"
    cmd = ["rsync", "-avz", "--progress", f"{store_path}/", remote]
    logger.info("Rsyncing to %s", remote)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"rsync failed: {result.stderr}")
    logger.info("Rsync complete")


@click.command()
@click.option("--api-url", default="https://tileripper.com", envvar="TILERIPPER_API_URL")
@click.option("--worker-key", required=True, envvar="TILERIPPER_WORKER_KEY")
@click.option("--vps-host", default=None, envvar="TILERIPPER_VPS_HOST")
@click.option("--poll-interval", default=30, type=int, help="Seconds between polls")
@click.option("--once", is_flag=True, help="Process one job and exit")
def main(api_url: str, worker_key: str, vps_host: str | None, poll_interval: int, once: bool):
    """TileRipper cluster worker — polls for jobs and runs ingestion."""
    client = httpx.Client(
        base_url=api_url,
        headers={"X-Worker-Key": worker_key},
        timeout=30.0,
    )

    logger.info("Worker started, polling %s every %ds", api_url, poll_interval)

    while True:
        try:
            job = claim_job(client)
        except httpx.HTTPError as e:
            logger.error("Failed to poll for jobs: %s", e)
            if once:
                sys.exit(1)
            time.sleep(poll_interval)
            continue

        if job is None:
            if once:
                logger.info("No pending jobs, exiting (--once)")
                return
            logger.debug("No pending jobs, sleeping %ds", poll_interval)
            time.sleep(poll_interval)
            continue

        job_id = job["id"]
        aoi_name = job["aoi_name"]
        logger.info("Claimed job %s for AOI %s", job_id, aoi_name)

        try:
            store_path = run_pipeline(job)

            # Rsync to VPS if configured
            if vps_host:
                rsync_to_vps(store_path, aoi_name, vps_host)

            report_complete(client, job_id, store_path)
        except Exception:
            tb = traceback.format_exc()
            logger.error("Job %s failed:\n%s", job_id, tb)
            try:
                report_failure(client, job_id, tb)
            except Exception as report_err:
                logger.error("Failed to report failure: %s", report_err)

        if once:
            return


if __name__ == "__main__":
    main()
