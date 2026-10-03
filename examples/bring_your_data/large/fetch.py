"""Download twelve real Sentinel-2 observations near Lake Mead into a local COG manifest.

Uses the existing Planetary Computer ingest helpers, not the hosted chronozarr demo.
This is a small adoption example, not a scientific change-detection product.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
from pathlib import Path

import numpy as np
import planetary_computer as pc
import rasterio
from rasterio.crs import CRS
from rasterio.transform import array_bounds
from rasterio.warp import transform_bounds

REPO = Path(__file__).resolve().parents[3]
BBOX = (-114.765, 36.105, -114.645, 36.205)
EPSG = 32611
MONTHS = tuple(f"2020-{m:02d}" for m in range(1, 13))


def fetch_sample(output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}; choose a new directory")
    # These are example-only helpers; no remote-data dependency enters the Python package.
    sys.path.insert(0, str(REPO / "examples/sentinel2_pc"))
    from catalog import PC_STAC_URL, S2_COLLECTION, search_scenes_by_month
    from mosaic import compute_target_grid, load_scene

    scenes = search_scenes_by_month(BBOX, "2020-01-01", "2020-12-31", max_cloud_pct=20)
    missing = set(MONTHS) - scenes.keys()
    if missing:
        raise ValueError(f"No observations for {sorted(missing)}")
    # Reuse exact acquisitions selected by the earlier small-grid run.
    pinned = json.loads(
        (REPO / "examples/bring_your_data/extended/results/source.json").read_text()
    )
    wanted = {s["id"] for s in pinned["scenes"]}
    selected = sorted(
        (scene for month in scenes.values() for scene in month if scene.item_id in wanted),
        key=lambda scene: scene.datetime,
    )
    if len(selected) != 12:
        raise ValueError("Pinned source acquisitions unavailable")
    transform, height, width = compute_target_grid(BBOX, EPSG)
    target_bounds = array_bounds(height, width, transform)
    with rasterio.open(pc.sign(selected[0].band_hrefs["B02"])) as source:
        west, south, east, north = transform_bounds(f"EPSG:{EPSG}", source.crs, *target_bounds)
        bounds = source.bounds
        assert bounds.left <= west and bounds.bottom <= south
        assert bounds.right >= east and bounds.top >= north
    output.mkdir(parents=True)
    provenance = {
        "catalog": PC_STAC_URL,
        "collection": S2_COLLECTION,
        "bbox_wgs84": BBOX,
        "crs": f"EPSG:{EPSG}",
        "shape": [height, width],
        "transform": list(transform),
        "prepared_cog_hash_scope": (
            "Complete prepared COG file; remote full assets not downloaded or hashed"
        ),
        "bands": ["blue", "green", "red", "nir"],
        "scl_valid_classes": [4, 5, 6, 7, 11],
        "processing": (
            "One acquisition per month, bilinear band resampling to a 10 m UTM grid; "
            "SCL nearest-neighbour validity; baseline offset correction; "
            "no compositing or gap filling."
        ),
        "scenes": [],
    }
    with (output / "observations.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["uri", "datetime"])
        writer.writeheader()
        for scene in selected:
            values, valid = load_scene(scene, transform, CRS.from_epsg(EPSG), height, width)
            if valid.mean() < 0.5:
                raise ValueError(
                    f"Only {valid.mean():.1%} valid pixels in {scene.item_id}; "
                    "refusing a mostly empty reference observation"
                )
            filename = f"{scene.datetime.isoformat()}.tif"
            with (
                rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True),
                rasterio.open(
                    output / filename,
                    "w",
                    driver="COG",
                    height=height,
                    width=width,
                    count=4,
                    dtype="uint16",
                    crs=f"EPSG:{EPSG}",
                    transform=transform,
                    compress="DEFLATE",
                ) as dest,
            ):
                dest.write(values)
                dest.write_mask(valid.astype(np.uint8) * 255)
                dest.descriptions = ("blue", "green", "red", "nir")
                dest.scales = (0.0001,) * 4
            writer.writerow({"uri": filename, "datetime": scene.datetime.isoformat()})
            provenance["scenes"].append(
                {
                    "id": scene.item_id,
                    "date": scene.datetime.isoformat(),
                    "mgrs_tile": scene.mgrs_tile,
                    "scene_cloud_pct": scene.cloud_cover,
                    "valid_fraction": float(valid.mean()),
                    "prepared_cog_sha256": hashlib.sha256(
                        (output / filename).read_bytes()
                    ).hexdigest(),
                    "unsigned_asset_hrefs": scene.asset_hrefs,
                    "processing_baseline": scene.processing_baseline,
                    "boa_offset": scene.boa_offset,
                }
            )
            print(f"Wrote {filename}: {height}x{width}, {valid.mean():.1%} valid", flush=True)
    (output / "source.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"Manifest: {output / 'observations.csv'}", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    fetch_sample(args.output)
