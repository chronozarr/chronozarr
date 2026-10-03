"""Download three real Sentinel-2 observations near Lake Mead into a local COG manifest.

Uses the existing Planetary Computer ingest helpers, not the hosted chronozarr demo.
This is a small adoption example, not a scientific change-detection product.
"""

from __future__ import annotations

import argparse
import csv
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

REPO = Path(__file__).resolve().parents[2]
BBOX = (-114.78, 36.12, -114.70, 36.20)
EPSG = 32611
MONTHS = ("2020-05", "2020-06", "2020-07")


def fetch_sample(output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}; choose a new directory")
    # These are example-only helpers; no remote-data dependency enters the Python package.
    sys.path.insert(0, str(REPO / "examples/sentinel2_pc"))
    from catalog import PC_STAC_URL, S2_COLLECTION, search_scenes_by_month
    from mosaic import compute_target_grid, load_scene

    scenes = search_scenes_by_month(BBOX, "2020-05-01", "2020-07-31", max_cloud_pct=20)
    missing = set(MONTHS) - scenes.keys()
    if missing:
        raise ValueError(f"No observations for {sorted(missing)}")
    # Keep the same tile across dates and prefer low scene-wide cloud percentage.
    tiles = set.intersection(*(set(s.mgrs_tile for s in scenes[m]) for m in MONTHS))
    if not tiles:
        raise ValueError("No common Sentinel-2 tile across the requested months")
    transform, height, width = compute_target_grid(BBOX, EPSG)
    target_bounds = array_bounds(height, width, transform)
    covering_tiles = []
    for candidate in sorted(tiles):
        scene = next(s for s in scenes[MONTHS[0]] if s.mgrs_tile == candidate)
        with rasterio.open(pc.sign(scene.band_hrefs["B02"])) as source:
            west, south, east, north = transform_bounds(f"EPSG:{EPSG}", source.crs, *target_bounds)
            bounds = source.bounds
            if (
                bounds.left <= west
                and bounds.bottom <= south
                and bounds.right >= east
                and bounds.top >= north
            ):
                covering_tiles.append(candidate)
    if not covering_tiles:
        raise ValueError("No tile fully covers the AOI; choose a smaller AOI or mosaic tiles")
    tile = min(
        covering_tiles,
        key=lambda t: sum(
            min(s.cloud_cover for s in scenes[m] if s.mgrs_tile == t) for m in MONTHS
        ),
    )
    selected = [
        min(
            (s for s in scenes[m] if s.mgrs_tile == tile),
            key=lambda s: (s.cloud_cover, s.datetime),
        )
        for m in MONTHS
    ]
    output.mkdir(parents=True)
    provenance = {
        "catalog": PC_STAC_URL,
        "collection": S2_COLLECTION,
        "bbox_wgs84": BBOX,
        "crs": f"EPSG:{EPSG}",
        "shape": [height, width],
        "bands": ["blue", "green", "red", "nir"],
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
